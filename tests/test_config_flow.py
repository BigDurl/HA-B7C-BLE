"""Exercise discovery and recovery using Home Assistant's config flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.b7c_ble.const import DOMAIN, PROVISIONING_UUID
from custom_components.b7c_ble.mesh.provisioning import ProvisioningError
from custom_components.b7c_ble.storage import mesh_store


@pytest.mark.asyncio
async def test_discovery_and_recovery_after_lost_provisioning_response(
    hass, enable_custom_integrations, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    address = "AA:BB:CC:DD:EE:FF"
    info = SimpleNamespace(address=address, service_data={PROVISIONING_UUID: b"04005-TEST01"})
    client = SimpleNamespace(disconnect=AsyncMock())

    class InterruptedProvisioner:
        def __init__(self, _client, on_device_key):
            self.save = on_device_key

        async def provision(self, _data):
            await self.save(bytes(16), 1)
            raise ProvisioningError("lost completion")

    with (
        patch("custom_components.b7c_ble.config_flow.bluetooth.async_discovered_service_info", return_value=[info]),
        patch("custom_components.b7c_ble.config_flow.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.config_flow.establish_connection", new=AsyncMock(return_value=client)),
        patch("custom_components.b7c_ble.config_flow.Provisioner", InterruptedProvisioner),
        patch("custom_components.b7c_ble.async_setup_entry", new=AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": address})
        assert result["step_id"] == "confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["errors"]["base"] == "provisioning_uncertain"
        state = await mesh_store(hass, address).async_load()
        assert state["device_key"] == bytes(16).hex()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] == "create_entry"
        assert result["data"] == {"address": address}
        assert client.disconnect.await_count == 1
