"""Home Assistant light entity for a B7c."""

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

    async def async_turn_on(self, **kwargs):
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

    async def async_turn_off(self, **kwargs):
        await self._controller.send(protocol.sleep(False))
        self._attr_is_on = False
        self.async_write_ha_state()
