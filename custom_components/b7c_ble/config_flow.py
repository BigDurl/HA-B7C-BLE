"""Discover and provision reset B7C bulbs."""

import logging
import secrets

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

from .const import DOMAIN, NODE_ADDRESS, PROVISIONING_UUID
from .mesh.provisioning import Provisioner, ProvisioningData, ProvisioningError
from .storage import mesh_store

_LOGGER = logging.getLogger(__name__)
_PROVISIONING: set[str] = set()


def is_b7c(info: BluetoothServiceInfoBleak) -> bool:
    """Identify a B7C by the product identifier in its device UUID."""
    return info.service_data.get(PROVISIONING_UUID, b"").startswith(b"04005-")


class B7CConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Commission a single B7C into its own mesh."""

    VERSION = 1

    def __init__(self):
        self._address = None

    async def async_step_bluetooth(self, discovery_info):
        if not is_b7c(discovery_info):
            return self.async_abort(reason="not_b7c")
        self._address = discovery_info.address.upper()
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()
        return await self.async_step_confirm()

    async def async_step_user(self, user_input=None):
        devices = {
            info.address.upper(): info
            for info in bluetooth.async_discovered_service_info(self.hass, True)
            if is_b7c(info)
        }
        if user_input is None:
            if not devices:
                return self.async_abort(reason="no_devices")
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({vol.Required("address"): vol.In(sorted(devices))}),
            )
        self._address = user_input["address"].upper()
        if self._address not in devices:
            return self.async_abort(reason="no_devices")
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="confirm", description_placeholders={"address": self._address})
        address = self._address
        if address in _PROVISIONING:
            return self.async_abort(reason="in_progress")
        _PROVISIONING.add(address)
        store = mesh_store(self.hass, address)
        try:
            state = await store.async_load()
            if state is None:
                state = {
                    "net_key": secrets.token_hex(16),
                    "app_key": secrets.token_hex(16),
                    "next_sequence": 1,
                    "configured": False,
                }
                await store.async_save(state)
            if "device_key" not in state:
                device = bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)
                if device is None:
                    return self.async_show_form(step_id="confirm", errors={"base": "cannot_connect"},
                                                description_placeholders={"address": address})
                client = await establish_connection(
                    BleakClientWithServiceCache, device, f"{DOMAIN}-{address}", timeout=25
                )
                try:
                    async def save_device_key(key, count):
                        if count != 1:
                            raise ProvisioningError(f"expected one element, received {count}")
                        state["device_key"] = key.hex()
                        state["provisioning"] = "uncertain"
                        await store.async_save(state)

                    await Provisioner(client, on_device_key=save_device_key).provision(
                        ProvisioningData(bytes.fromhex(state["net_key"]), 0, 0, NODE_ADDRESS)
                    )
                finally:
                    await client.disconnect()
                state["provisioning"] = "complete"
                await store.async_save(state)
            return self.async_create_entry(title=f"Aputure B7C ({address[-8:]})", data={"address": address})
        except (BleakError, ProvisioningError, TimeoutError, OSError) as err:
            _LOGGER.warning("B7C provisioning failed for %s: %s", address, err)
            saved = await store.async_load()
            reason = "provisioning_uncertain" if saved and "device_key" in saved else "provisioning_failed"
            return self.async_show_form(step_id="confirm", errors={"base": reason},
                                        description_placeholders={"address": address})
        finally:
            _PROVISIONING.discard(address)
