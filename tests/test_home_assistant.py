"""Verify the integration loads and exposes a controllable light."""

import asyncio
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


@pytest.mark.asyncio
async def test_concurrent_light_services_coalesce(hass, enable_custom_integrations, mock_bluetooth, tmp_path):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    await mesh_store(hass, address).async_save({
        "next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
        "device_key": "22" * 16, "configured": True,
    })
    entry = MockConfigEntry(domain=DOMAIN, title="Test B7c", unique_id=address, data={"address": address})
    entry.add_to_hass(hass)
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(*_payloads):
        if not started.is_set():
            started.set()
            await release.wait()

    with patch("custom_components.b7c_ble.controller.B7cController.send", new=AsyncMock(side_effect=send)) as send_mock:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        async def change(brightness):
            await hass.services.async_call(
                "light", "turn_on", {"entity_id": "light.test_b7c", "brightness": brightness},
                blocking=True,
            )

        first = asyncio.create_task(change(50))
        await started.wait()
        second = asyncio.create_task(change(100))
        third = asyncio.create_task(change(200))
        # Allow service handlers to reach the blocked entity while the first send is in flight.
        for _ in range(10):
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second, third)
        assert send_mock.await_count == 2
        assert hass.states.get("light.test_b7c").attributes["brightness"] == 200
        controller = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert controller._closed
