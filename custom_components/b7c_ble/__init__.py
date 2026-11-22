"""Aputure B7C BLE integration."""

from bleak.exc import BleakError
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from .controller import B7CController
from .mesh.session import MeshSessionError
from .storage import mesh_store

PLATFORMS = [Platform.LIGHT]


async def async_setup_entry(hass, entry):
    """Load a commissioned bulb and finish configuration if necessary."""
    address = entry.data["address"]
    store = mesh_store(hass, address)
    state = await store.async_load()
    if not state or "device_key" not in state:
        raise ConfigEntryNotReady("B7C mesh state is missing; see recovery instructions")
    controller = B7CController(hass, address, store, state)
    try:
        await controller.configure()
    except (BleakError, MeshSessionError, HomeAssistantError, TimeoutError, OSError) as err:
        raise ConfigEntryNotReady(f"Could not configure B7C {address}: {err}") from err
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, controller.async_close)
    )
    return True


async def async_unload_entry(hass, entry):
    """Unload the light without deleting its recovery keys."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
