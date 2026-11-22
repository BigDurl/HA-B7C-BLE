"""Aputure B7c BLE integration."""

from bleak.exc import BleakError
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from .controller import B7cController
from .mesh.session import MeshSessionError
from .storage import mesh_store

PLATFORMS = [Platform.LIGHT]


async def async_setup_entry(hass, entry):
    """Load a commissioned bulb and finish configuration if necessary."""
    address = entry.data["address"]
    store = mesh_store(hass, address)
    state = await store.async_load()
    if not state or "device_key" not in state:
        raise ConfigEntryNotReady("B7c mesh state is missing; see recovery instructions")
    controller = B7cController(hass, address, store, state)
    try:
        await controller.configure()
    except (BleakError, MeshSessionError, HomeAssistantError, TimeoutError, OSError) as err:
        raise ConfigEntryNotReady(f"Could not configure B7c {address}: {err}") from err
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    """Unload the light without deleting its recovery keys."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
