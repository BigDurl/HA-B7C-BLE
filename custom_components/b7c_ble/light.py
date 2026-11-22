"""Home Assistant light entity for a B7c."""

import asyncio
from collections import deque

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .mesh import protocol


def hsi_payload(hue, saturation, intensity):
    return protocol.pack([
        (0, 8), (0, 38), (round(saturation), 7), (round(hue), 9),
        (intensity, 10), (1, 7), (1, 1),
    ])


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([B7cLight(entry)])


class B7cLight(LightEntity):
    """An optimistic light; the bulb does not acknowledge light commands."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.HS}
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 10000

    def __init__(self, entry):
        self._controller = entry.runtime_data
        self._attr_unique_id = entry.unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name=entry.title,
            manufacturer="Aputure",
            model="Accent B7c",
        )
        self._attr_is_on = None
        self._attr_brightness = 128
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_color_temp_kelvin = 5600
        self._attr_hs_color = (0.0, 100.0)
        self._pending = deque()
        self._worker = None

    async def async_turn_on(self, **kwargs):
        await self._enqueue(True, kwargs)

    async def async_turn_off(self, **kwargs):
        await self._enqueue(False, kwargs)

    async def _enqueue(self, turn_on, kwargs):
        future = asyncio.get_running_loop().create_future()
        if turn_on and self._pending and self._pending[-1][0]:
            _, pending, waiters = self._pending[-1]
            if ATTR_HS_COLOR in kwargs:
                pending.pop(ATTR_COLOR_TEMP_KELVIN, None)
            elif ATTR_COLOR_TEMP_KELVIN in kwargs:
                pending.pop(ATTR_HS_COLOR, None)
            pending.update(kwargs)
            waiters.append(future)
        else:
            self._pending.append((turn_on, dict(kwargs), [future]))
        if self._worker is None:
            self._worker = asyncio.create_task(self._process_pending())
        await future

    async def _process_pending(self):
        waiters = []
        operation = None
        try:
            while self._pending:
                turn_on, kwargs, waiters = self._pending.popleft()
                operation = asyncio.create_task(
                    self._turn_on(**kwargs) if turn_on else self._turn_off()
                )
                await asyncio.wait({operation})
                error = None if operation.cancelled() else operation.exception()
                for future in waiters:
                    if future.done():
                        continue
                    if operation.cancelled():
                        future.cancel()
                    elif error is not None:
                        future.set_exception(error)
                    else:
                        future.set_result(None)
                operation = None
                waiters = []
        finally:
            if operation is not None:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            for future in waiters:
                if not future.done():
                    future.cancel()
            for _, _, pending_waiters in self._pending:
                for future in pending_waiters:
                    if not future.done():
                        future.cancel()
            self._pending.clear()
            self._worker = None

    async def async_will_remove_from_hass(self):
        if self._worker is not None:
            await self._worker
        await super().async_will_remove_from_hass()

    async def _turn_on(self, **kwargs):
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness)
        intensity = round(brightness * 1000 / 255)
        payloads = []
        if self._attr_is_on is not True:
            payloads.append(protocol.sleep(True))
        if ATTR_HS_COLOR in kwargs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            hs = (float(hue), float(saturation))
            payload = hsi_payload(hue, saturation, intensity)
            mode = ColorMode.HS
            kelvin = self._attr_color_temp_kelvin
        elif ATTR_COLOR_TEMP_KELVIN in kwargs or self._attr_color_mode == ColorMode.COLOR_TEMP:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN, self._attr_color_temp_kelvin)
            kelvin = max(2000, min(10000, kelvin))
            payload = protocol.cct(intensity, round(kelvin / 10))
            mode = ColorMode.COLOR_TEMP
            hs = self._attr_hs_color
        else:
            hs = self._attr_hs_color
            payload = hsi_payload(*hs, intensity)
            mode = ColorMode.HS
            kelvin = self._attr_color_temp_kelvin
        await self._controller.send(*payloads, payload)
        self._attr_is_on = True
        self._attr_brightness = brightness
        self._attr_color_mode = mode
        self._attr_color_temp_kelvin = kelvin
        self._attr_hs_color = hs
        self.async_write_ha_state()

    async def _turn_off(self):
        await self._controller.send(protocol.sleep(False))
        self._attr_is_on = False
        self.async_write_ha_state()
