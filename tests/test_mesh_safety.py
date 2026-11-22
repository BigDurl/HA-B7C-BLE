"""Tests for provisioning and durable mesh state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.b7c_ble.controller import B7cController, ReservedSession
from custom_components.b7c_ble.light import B7cLight, hsi_payload
from custom_components.b7c_ble.mesh import protocol
from custom_components.b7c_ble.mesh.crypto import EcdhKeyPair, PRCK, aes_cmac, k1, s1
from custom_components.b7c_ble.mesh.provisioning import Provisioner, ProvisioningData
from custom_components.b7c_ble.storage import MeshStore, validate_state


class FakeHass:
    def __init__(self, path):
        self.config = SimpleNamespace(config_dir=str(path), path=lambda *parts: str(path.joinpath(*parts)))

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def test_invalid_sequence_is_rejected():
    with pytest.raises(ValueError, match="sequence"):
        validate_state({"next_sequence": 0, "net_key": "00" * 16, "app_key": "11" * 16})


@pytest.mark.asyncio
async def test_storage_is_private_and_write_failure_is_not_hidden(tmp_path):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 2049, "net_key": "00" * 16, "app_key": "11" * 16}
    await store.async_save(state)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert await store.async_load() == state
    with patch("custom_components.b7c_ble.storage.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError, match="disk failure"):
            await store.async_save({**state, "next_sequence": 4097})
    assert await store.async_load() == state


@pytest.mark.asyncio
async def test_sequence_is_reserved_before_any_mesh_send(tmp_path):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
             "device_key": "22" * 16}
    client = SimpleNamespace(disconnect=AsyncMock(), start_notify=AsyncMock(),
                             stop_notify=AsyncMock(), write_gatt_char=AsyncMock())
    with (
        patch("custom_components.b7c_ble.controller.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.controller.establish_connection", new=AsyncMock(return_value=client)),
        patch.object(store, "async_save", new=AsyncMock(side_effect=OSError("disk failure"))),
    ):
        controller = B7cController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        with pytest.raises(OSError, match="disk failure"):
            async with controller._session():
                pass
        client.write_gatt_char.assert_not_awaited()
        client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_sequence_reservation_does_not_wrap():
    from custom_components.b7c_ble.mesh.session import MeshCredentials, MeshSessionError

    credentials = MeshCredentials(bytes(16), bytes(16))
    session = ReservedSession(None, credentials, (1 << 24) - 1, 1 << 24)
    assert session._next_seq() == (1 << 24) - 1
    with pytest.raises(MeshSessionError):
        session._next_seq()


@pytest.mark.parametrize("fail_save", [False, True])
@pytest.mark.asyncio
async def test_device_key_is_saved_before_provisioning_data(fail_save):
    device = EcdhKeyPair.generate()
    device_random = bytes(range(16))
    saved = []
    sent = []

    async def save(key, count):
        if fail_save:
            raise OSError("disk failure")
        saved.append((key, count))

    provisioner = Provisioner(None, on_device_key=save)
    provisioner._bearer.start = AsyncMock()
    provisioner._bearer.stop = AsyncMock()

    async def send(_msg_type, pdu):
        kind = pdu[0]
        sent.append(kind)
        if kind == 0:
            provisioner._on_pdu(3, b"\x01" + bytes.fromhex("0100010000000000000000"))
        elif kind == 3:
            provisioner._on_pdu(3, b"\x03" + device.public_key_bytes)
        elif kind == 5:
            secret = device.shared_secret(provisioner._keypair.public_key_bytes)
            salt = s1(provisioner._confirmation_inputs())
            confirmation = aes_cmac(k1(secret, salt, PRCK), device_random + bytes(16))
            provisioner._on_pdu(3, b"\x05" + confirmation)
        elif kind == 6:
            provisioner._on_pdu(3, b"\x06" + device_random)
        elif kind == 7:
            assert len(saved) == 1
            provisioner._on_pdu(3, b"\x08")

    provisioner._bearer.send = send
    data = ProvisioningData(bytes(16), 0, 0, 2)
    if fail_save:
        with pytest.raises(OSError, match="disk failure"):
            await provisioner.provision(data)
        assert 7 not in sent
    else:
        result = await provisioner.provision(data)
        assert result.device_key == saved[0][0]


def test_color_payload_matches_tested_reference():
    assert hsi_payload(120, 100, 400).hex() == "0d0000000000190f6481"
    assert protocol.cct(800, 560).hex() == "af01000000400123c882"
    assert protocol.sleep(True).hex() == "8d00000000000000018c"
    assert protocol.sleep(False).hex() == "8c00000000000000008c"


@pytest.mark.asyncio
async def test_light_does_not_update_optimistic_state_after_failed_send():
    controller = SimpleNamespace(send=AsyncMock(side_effect=OSError("Bluetooth failed")))
    entry = SimpleNamespace(unique_id="AA:BB:CC:DD:EE:FF", runtime_data=controller, title="Test")
    light = B7cLight(entry)
    with patch.object(light, "async_write_ha_state"):
        with pytest.raises(OSError, match="Bluetooth failed"):
            await light.async_turn_on(hs_color=(120, 100), brightness=102)
        assert light.is_on is None
        controller.send.side_effect = None
        await light.async_turn_on(hs_color=(120, 100), brightness=102)
    assert light.is_on is True
    assert light.hs_color == (120.0, 100.0)
    assert light.brightness == 102
