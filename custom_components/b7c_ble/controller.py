"""Commission and control one B7c over Bluetooth Mesh."""

import asyncio
import logging
from contextlib import asynccontextmanager

from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, NODE_ADDRESS, SEQUENCE_BLOCK, SEQUENCE_LIMIT, SOURCE_ADDRESS
from .mesh import protocol
from .mesh.proxy import CHAR_PROXY_DATA_OUT
from .mesh.session import MeshCredentials, MeshSession, MeshSessionError

_LOGGER = logging.getLogger(__name__)
IDLE_DISCONNECT_SECONDS = 60
MAX_ACCESS_SEGMENTS = 32


class ReservedSession(MeshSession):
    """Fail closed if a reserved block is exhausted."""

    def __init__(self, client, credentials, start, end):
        super().__init__(client, credentials, sequence=start)
        self.client = client
        self._end = end

    def _next_seq(self):
        if self._sequence >= self._end:
            raise MeshSessionError("reserved mesh sequence block exhausted")
        sequence = self._sequence
        self._sequence += 1
        return sequence


class B7cController:
    """A device-local mesh with durable sequence reservations."""

    def __init__(self, hass, address, store, state):
        self.hass = hass
        self.address = address
        self.store = store
        self.state = state
        self._lock = asyncio.Lock()
        self._active_context = None
        self._active_session = None
        self._idle_handle = None
        self._idle_generation = 0
        self._closed = False

    @asynccontextmanager
    async def _session(self):
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise HomeAssistantError(f"B7c {self.address} is not available over Bluetooth")
        for attempt in range(2):
            client = await establish_connection(
                BleakClientWithServiceCache, device, f"{DOMAIN}-{self.address}",
                timeout=25, use_services_cache=attempt == 0,
            )
            try:
                start = self.state["next_sequence"]
                end = start + SEQUENCE_BLOCK
                if end > SEQUENCE_LIMIT:
                    raise HomeAssistantError("B7c mesh sequence space exhausted; reset and re-add the bulb")
                self.state["next_sequence"] = end
                await self.store.async_save(self.state)
                device_key = bytes.fromhex(self.state["device_key"])
                credentials = MeshCredentials(
                    net_key=bytes.fromhex(self.state["net_key"]),
                    app_key=bytes.fromhex(self.state["app_key"]),
                    provisioner_address=SOURCE_ADDRESS,
                    device_keys={NODE_ADDRESS: device_key},
                )
                session = ReservedSession(client, credentials, start, end)
                try:
                    await session.start()
                except BleakCharacteristicNotFoundError as err:
                    if attempt or str(err.char_specifier).lower() != CHAR_PROXY_DATA_OUT:
                        raise
                    _LOGGER.warning(
                        "B7c %s is missing the mesh proxy characteristic; reconnecting without cached services",
                        self.address,
                    )
                    continue
                try:
                    await session.set_proxy_filter([SOURCE_ADDRESS])
                    yield session
                finally:
                    await session.stop()
                return
            finally:
                await client.disconnect()

    async def configure(self):
        async with self._lock:
            if self.state.get("configured"):
                return
            async with self._session() as session:
                key = bytes.fromhex(self.state["device_key"])
                composition = await session.get_composition_data(NODE_ADDRESS, key)
                models = [(element.address, company, model)
                          for element in composition.elements
                          for company, model in element.vendor_models]
                if not models:
                    raise HomeAssistantError("B7c returned no vendor model")
                await session.add_app_key(NODE_ADDRESS, key)
                for element, company, model in models:
                    await session.bind_model(NODE_ADDRESS, key, element, model, company_id=company)
            self.state["configured"] = True
            self.state["provisioning"] = "complete"
            await self.store.async_save(self.state)

    def _cancel_idle(self):
        self._idle_generation += 1
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    async def _disconnect(self):
        self._cancel_idle()
        context = self._active_context
        self._active_context = None
        self._active_session = None
        if context is not None:
            await context.__aexit__(None, None, None)

    async def _disconnect_idle(self, generation):
        async with self._lock:
            if self._idle_handle is None or generation != self._idle_generation:
                return
            try:
                await self._disconnect()
            except (BleakError, TimeoutError, OSError) as err:
                _LOGGER.warning("Could not disconnect idle B7c %s: %s", self.address, err)

    def _schedule_idle_disconnect(self):
        self._cancel_idle()
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(
            IDLE_DISCONNECT_SECONDS, self._start_idle_disconnect, self._idle_generation
        )

    def _start_idle_disconnect(self, generation):
        self.hass.async_create_task(self._disconnect_idle(generation))

    async def async_close(self, _event=None):
        """Close the active connection during unload or shutdown."""
        async with self._lock:
            self._closed = True
            await self._disconnect()

    async def send(self, *payloads):
        async with self._lock:
            if self._closed:
                raise HomeAssistantError(f"B7c {self.address} is unloading")
            self._cancel_idle()
            sent = False
            try:
                session = self._active_session
                if session is not None and (
                    not session.client.is_connected
                    or session._end - session.sequence < MAX_ACCESS_SEGMENTS
                ):
                    await self._disconnect()
                    session = None
                if session is None:
                    context = self._session()
                    session = await context.__aenter__()
                    self._active_context = context
                    self._active_session = session
                for payload in payloads:
                    if session._end - session.sequence < MAX_ACCESS_SEGMENTS:
                        await self._disconnect()
                        context = self._session()
                        session = await context.__aenter__()
                        self._active_context = context
                        self._active_session = session
                    await session.send_access(NODE_ADDRESS, protocol.VENDOR_OPCODE, payload)
                sent = True
            except (BleakError, MeshSessionError, TimeoutError, OSError) as err:
                raise HomeAssistantError(f"Could not send to B7c {self.address}: {err}") from err
            finally:
                if not sent:
                    try:
                        await self._disconnect()
                    except (BleakError, TimeoutError, OSError) as cleanup_err:
                        _LOGGER.warning("Could not disconnect B7c %s: %s", self.address, cleanup_err)
            self._schedule_idle_disconnect()
