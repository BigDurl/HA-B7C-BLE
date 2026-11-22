"""Verify the integration loads and exposes a controllable light."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.b7c_ble.const import DOMAIN
from custom_components.b7c_ble.storage import mesh_store


@pytest.mark.asyncio
async def test_setup_and_light_service(hass, enable_custom_integrations, mock_bluetooth, tmp_path):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    await mesh_store(hass, address).async_save({
        "next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
        "device_key": "22" * 16, "configured": True,
    })
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test B7c", unique_id=address, data={"address": address}
    )
    entry.add_to_hass(hass)
    with patch("custom_components.b7c_ble.controller.B7cController.send", new=AsyncMock()) as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        state = hass.states.get("light.test_b7c")
        assert state is not None
        await hass.services.async_call(
            "light", "turn_on",
            {"entity_id": "light.test_b7c", "hs_color": [120, 100], "brightness": 102},
            blocking=True,
        )
        send.assert_awaited_once()
        assert hass.states.get("light.test_b7c").state == "on"
