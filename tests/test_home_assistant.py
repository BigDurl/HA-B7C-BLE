"""Verify the integration loads and exposes a controllable light."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import async_get as get_restore_data
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.b7c_ble.const import DOMAIN
from custom_components.b7c_ble.light import hsi_payload
from custom_components.b7c_ble.mesh import protocol
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


@pytest.mark.parametrize(
    ("mode", "color", "payload"),
    [
        ("hs", {"hs_color": [120, 80]}, hsi_payload(120, 80, round(180 * 1000 / 255))),
        ("color_temp", {"color_temp_kelvin": 4200}, protocol.cct(round(180 * 1000 / 255), 420)),
    ],
)
@pytest.mark.asyncio
async def test_startup_reapplies_saved_on_state(
    hass, enable_custom_integrations, mock_bluetooth, tmp_path, mode, color, payload
):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    await mesh_store(hass, address).async_save({
        "next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
        "device_key": "22" * 16, "configured": True,
    })
    mock_restore_cache_with_extra_data(hass, [(
        State("light.test_b7c", "on"),
        {"brightness": 180, "color_mode": mode, **color},
    )])
    entry = MockConfigEntry(domain=DOMAIN, title="Test B7c", unique_id=address, data={"address": address})
    entry.add_to_hass(hass)
    with patch("custom_components.b7c_ble.controller.B7cController.send", new=AsyncMock()) as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        send.assert_awaited_once_with(protocol.sleep(True), payload)
        state = hass.states.get("light.test_b7c")
        assert state.state == "on"
        assert state.attributes["brightness"] == 180
        assert state.attributes["color_mode"] == mode


@pytest.mark.asyncio
async def test_off_state_retains_color_and_brightness_across_reload(
    hass, enable_custom_integrations, mock_bluetooth, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    await mesh_store(hass, address).async_save({
        "next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
        "device_key": "22" * 16, "configured": True,
    })
    entry = MockConfigEntry(domain=DOMAIN, title="Test B7c", unique_id=address, data={"address": address})
    entry.add_to_hass(hass)
    with patch("custom_components.b7c_ble.controller.B7cController.send", new=AsyncMock()) as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": "light.test_b7c", "hs_color": [240, 75], "brightness": 150},
            blocking=True,
        )
        await hass.services.async_call("light", "turn_off", {"entity_id": "light.test_b7c"}, blocking=True)
        restore_data = get_restore_data(hass)
        await restore_data.async_dump_states()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await restore_data.async_load()
        send.reset_mock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        send.assert_awaited_once_with(protocol.sleep(False))
        assert hass.states.get("light.test_b7c").state == "off"
        send.reset_mock()
        await hass.services.async_call("light", "turn_on", {"entity_id": "light.test_b7c"}, blocking=True)
        send.assert_awaited_once_with(
            protocol.sleep(True), hsi_payload(240, 75, round(150 * 1000 / 255))
        )


@pytest.mark.asyncio
async def test_startup_does_not_apply_missing_or_invalid_on_settings(
    hass, enable_custom_integrations, mock_bluetooth, tmp_path, caplog
):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    await mesh_store(hass, address).async_save({
        "next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
        "device_key": "22" * 16, "configured": True,
    })
    mock_restore_cache(hass, [State("light.test_b7c", "on", {"brightness": 180})])
    entry = MockConfigEntry(domain=DOMAIN, title="Test B7c", unique_id=address, data={"address": address})
    entry.add_to_hass(hass)
    with patch("custom_components.b7c_ble.controller.B7cController.send", new=AsyncMock()) as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        send.assert_not_awaited()
        assert "saved light settings are missing or invalid" in caplog.text


@pytest.mark.asyncio
async def test_startup_restore_failure_is_logged(
    hass, enable_custom_integrations, mock_bluetooth, tmp_path, caplog
):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    await mesh_store(hass, address).async_save({
        "next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
        "device_key": "22" * 16, "configured": True,
    })
    mock_restore_cache_with_extra_data(hass, [(
        State("light.test_b7c", "on"),
        {"brightness": 180, "color_mode": "hs", "hs_color": [120, 80]},
    )])
    entry = MockConfigEntry(domain=DOMAIN, title="Test B7c", unique_id=address, data={"address": address})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.b7c_ble.controller.B7cController.send",
        new=AsyncMock(side_effect=HomeAssistantError("Bluetooth failed")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert "Could not restore B7c light.test_b7c after startup: Bluetooth failed" in caplog.text


@pytest.mark.asyncio
async def test_user_change_before_startup_prevents_stale_restore(
    hass, enable_custom_integrations, mock_bluetooth, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    await mesh_store(hass, address).async_save({
        "next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
        "device_key": "22" * 16, "configured": True,
    })
    mock_restore_cache_with_extra_data(hass, [(
        State("light.test_b7c", "on"),
        {"brightness": 180, "color_mode": "hs", "hs_color": [120, 80]},
    )])
    entry = MockConfigEntry(domain=DOMAIN, title="Test B7c", unique_id=address, data={"address": address})
    entry.add_to_hass(hass)
    callbacks = []
    with (
        patch("custom_components.b7c_ble.controller.B7cController.send", new=AsyncMock()) as send,
        patch(
            "custom_components.b7c_ble.light.async_at_started",
            side_effect=lambda _hass, callback: callbacks.append(callback) or (lambda: None),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.services.async_call("light", "turn_off", {"entity_id": "light.test_b7c"}, blocking=True)
        await callbacks[0](hass)
        send.assert_awaited_once_with(protocol.sleep(False))
