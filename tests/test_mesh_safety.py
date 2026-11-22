"""Tests for provisioning and durable mesh state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from homeassistant.exceptions import HomeAssistantError

from custom_components.b7c_ble.controller import B7CController, ReservedSession
from custom_components.b7c_ble.light import B7CLight, hsi_payload
from custom_components.b7c_ble.mesh import protocol
from custom_components.b7c_ble.mesh.crypto import EcdhKeyPair, PRCK, aes_cmac, k1, s1
from custom_components.b7c_ble.mesh.pdu import ProxyReassembler
from custom_components.b7c_ble.mesh.provisioning import Provisioner, ProvisioningData
from custom_components.b7c_ble.mesh.proxy import (
    CHAR_PROXY_DATA_OUT,
    MeshGattBearer,
)
from custom_components.b7c_ble.storage import MeshStore, validate_state


class FakeHass:
    def __init__(self, path):
        self.config = SimpleNamespace(config_dir=str(path), path=lambda *parts: str(path.joinpath(*parts)))

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


def fake_services(size=20):
    return SimpleNamespace(
        get_characteristic=lambda _: SimpleNamespace(max_write_without_response_size=size)
    )


@pytest.mark.parametrize(("size", "payload_length"), [(20, 70), (64, 70), (64, 10)])
@pytest.mark.parametrize("provisioning", [False, True])
@pytest.mark.asyncio
async def test_bearer_uses_characteristic_write_limit_without_querying_mtu(size, payload_length, provisioning):
    class Client:
        services = fake_services(size)
        write_gatt_char = AsyncMock()

        @property
        def mtu_size(self):
            raise AssertionError("MTU must not be queried")

    client = Client()
    bearer = (
        MeshGattBearer.provisioning(client, lambda *_: None)
        if provisioning else MeshGattBearer.proxy(client, lambda *_: None)
    )
    msg_type = 3 if provisioning else 0
    payload = bytes(range(payload_length))
    await bearer.send(msg_type, payload)
    chunks = [call.args[1] for call in client.write_gatt_char.await_args_list]
    assert all(len(chunk) <= size for chunk in chunks)
    assert len(chunks) == (len(payload) + size - 2) // (size - 1)
    reassembler = ProxyReassembler()
    result = None
    for chunk in chunks:
        result = reassembler.push(chunk)
    assert result == (msg_type, payload)


@pytest.mark.asyncio
async def test_bearer_rejects_missing_input_characteristic():
    client = SimpleNamespace(services=SimpleNamespace(get_characteristic=lambda _: None))
    with pytest.raises(BleakCharacteristicNotFoundError):
        await MeshGattBearer.proxy(client, lambda *_: None).send(0, b"test")


@pytest.mark.asyncio
async def test_bearer_rejects_invalid_write_limit():
    client = SimpleNamespace(services=fake_services(1))
    with pytest.raises(BleakError, match="Invalid mesh GATT write size"):
        await MeshGattBearer.proxy(client, lambda *_: None).send(0, b"test")


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
async def test_cancelled_save_waits_for_durable_write(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedHass(FakeHass):
        async def async_add_executor_job(self, func, *args):
            started.set()
            await release.wait()
            return func(*args)

    store = MeshStore(DelayedHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 2049, "net_key": "00" * 16, "app_key": "11" * 16}
    task = asyncio.create_task(store.async_save(state))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store._load() == state


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
        controller = B7CController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        with pytest.raises(OSError, match="disk failure"):
            async with controller._session():
                pass
        client.write_gatt_char.assert_not_awaited()
        client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("retry_succeeds", [True, False])
@pytest.mark.asyncio
async def test_missing_proxy_characteristic_retries_without_cached_services(tmp_path, retry_succeeds):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
             "device_key": "22" * 16}
    first = SimpleNamespace(
        disconnect=AsyncMock(),
        start_notify=AsyncMock(side_effect=BleakCharacteristicNotFoundError(CHAR_PROXY_DATA_OUT)),
    )
    second = SimpleNamespace(
        disconnect=AsyncMock(),
        start_notify=AsyncMock(
            side_effect=None if retry_succeeds else BleakCharacteristicNotFoundError(CHAR_PROXY_DATA_OUT)
        ),
        stop_notify=AsyncMock(), write_gatt_char=AsyncMock(), services=fake_services(),
    )
    connect = AsyncMock(side_effect=[first, second])
    with (
        patch("custom_components.b7c_ble.controller.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.controller.establish_connection", new=connect),
    ):
        controller = B7CController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        if retry_succeeds:
            async with controller._session():
                pass
        else:
            with pytest.raises(BleakCharacteristicNotFoundError):
                async with controller._session():
                    pass
    assert [call.kwargs["use_services_cache"] for call in connect.await_args_list] == [True, False]
    first.disconnect.assert_awaited_once()
    second.disconnect.assert_awaited_once()
    assert (await store.async_load())["next_sequence"] == state["next_sequence"]


@pytest.mark.asyncio
async def test_other_missing_characteristic_is_not_retried(tmp_path):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
             "device_key": "22" * 16}
    client = SimpleNamespace(
        disconnect=AsyncMock(),
        start_notify=AsyncMock(side_effect=BleakCharacteristicNotFoundError("other")),
    )
    connect = AsyncMock(return_value=client)
    with (
        patch("custom_components.b7c_ble.controller.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.controller.establish_connection", new=connect),
    ):
        controller = B7CController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        with pytest.raises(BleakCharacteristicNotFoundError):
            async with controller._session():
                pass
    connect.assert_awaited_once()
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_characteristic_after_session_start_is_not_retried(tmp_path):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
             "device_key": "22" * 16}
    client = SimpleNamespace(
        disconnect=AsyncMock(), start_notify=AsyncMock(), stop_notify=AsyncMock(),
        write_gatt_char=AsyncMock(), services=fake_services(),
    )
    connect = AsyncMock(return_value=client)
    with (
        patch("custom_components.b7c_ble.controller.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.controller.establish_connection", new=connect),
    ):
        controller = B7CController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        with pytest.raises(BleakCharacteristicNotFoundError):
            async with controller._session():
                raise BleakCharacteristicNotFoundError(CHAR_PROXY_DATA_OUT)
    connect.assert_awaited_once()
    client.stop_notify.assert_awaited_once()
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
    light = B7CLight(entry)
    with patch.object(light, "async_write_ha_state"):
        with pytest.raises(OSError, match="Bluetooth failed"):
            await light.async_turn_on(hs_color=(120, 100), brightness=102)
        assert light.is_on is None
        controller.send.side_effect = None
        await light.async_turn_on(hs_color=(120, 100), brightness=102)
    assert light.is_on is True
    assert light.hs_color == (120.0, 100.0)
    assert light.brightness == 102


@pytest.mark.asyncio
async def test_connection_reused_then_closed_when_idle(tmp_path):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
             "device_key": "22" * 16}
    client = SimpleNamespace(
        is_connected=True, disconnect=AsyncMock(), start_notify=AsyncMock(),
        stop_notify=AsyncMock(), write_gatt_char=AsyncMock(), services=fake_services(),
    )
    connect = AsyncMock(return_value=client)
    with (
        patch("custom_components.b7c_ble.controller.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.controller.establish_connection", new=connect),
        patch("custom_components.b7c_ble.controller.IDLE_DISCONNECT_SECONDS", 0.02),
    ):
        controller = B7CController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        await controller.send(protocol.sleep(True))
        sequence = controller._active_session.sequence
        old_generation = controller._idle_generation
        await controller.send(protocol.sleep(False))
        assert controller._active_session.sequence > sequence
        await controller._disconnect_idle(old_generation)
        connect.assert_awaited_once()
        client.disconnect.assert_not_awaited()
        await asyncio.sleep(0.06)
        client.disconnect.assert_awaited_once()
        await controller.send(protocol.sleep(True))
        assert connect.await_count == 2
        assert (await store.async_load())["next_sequence"] == state["next_sequence"]
        await controller.async_close()
        assert client.disconnect.await_count == 2


@pytest.mark.asyncio
async def test_connection_reconnects_after_drop_and_write_failure(tmp_path):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
             "device_key": "22" * 16}
    clients = [
        SimpleNamespace(is_connected=True, disconnect=AsyncMock(), start_notify=AsyncMock(),
                        stop_notify=AsyncMock(), write_gatt_char=AsyncMock(), services=fake_services())
        for _ in range(3)
    ]
    connect = AsyncMock(side_effect=clients)
    with (
        patch("custom_components.b7c_ble.controller.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.controller.establish_connection", new=connect),
    ):
        controller = B7CController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        await controller.send(protocol.sleep(True))
        clients[0].is_connected = False
        await controller.send(protocol.sleep(True))
        assert connect.await_count == 2
        clients[1].write_gatt_char.side_effect = BleakError("write failed")
        with pytest.raises(HomeAssistantError, match="write failed"):
            await controller.send(protocol.sleep(False))
        clients[1].disconnect.assert_awaited_once()
        await controller.send(protocol.sleep(False))
        assert connect.await_count == 3
        await controller.async_close()


@pytest.mark.asyncio
async def test_connection_reopens_before_reserved_sequences_are_exhausted(tmp_path):
    store = MeshStore(FakeHass(tmp_path), "AA:BB:CC:DD:EE:FF")
    state = {"next_sequence": 1, "net_key": "00" * 16, "app_key": "11" * 16,
             "device_key": "22" * 16}
    client = SimpleNamespace(
        is_connected=True, disconnect=AsyncMock(), start_notify=AsyncMock(),
        stop_notify=AsyncMock(), write_gatt_char=AsyncMock(), services=fake_services(),
    )
    connect = AsyncMock(return_value=client)
    with (
        patch("custom_components.b7c_ble.controller.bluetooth.async_ble_device_from_address", return_value=object()),
        patch("custom_components.b7c_ble.controller.establish_connection", new=connect),
    ):
        controller = B7CController(store._hass, "AA:BB:CC:DD:EE:FF", store, state)
        await controller.send(protocol.sleep(True))
        end = controller._active_session._end
        controller._active_session._sequence = end - 31
        await controller.send(protocol.sleep(False))
        assert connect.await_count == 2
        assert controller._active_session.sequence > end
        assert (await store.async_load())["next_sequence"] == controller._active_session._end
        await controller.async_close()


@pytest.mark.asyncio
async def test_adjustment_queue_merges_pending_values_and_preserves_power_order():
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(*_payloads):
        if not started.is_set():
            started.set()
            await release.wait()

    controller = SimpleNamespace(send=AsyncMock(side_effect=send))
    entry = SimpleNamespace(unique_id="AA:BB:CC:DD:EE:FF", runtime_data=controller, title="Test")
    light = B7CLight(entry)
    with patch.object(light, "async_write_ha_state"):
        first = asyncio.create_task(light.async_turn_on(brightness=50))
        await started.wait()
        pending = [
            asyncio.create_task(light.async_turn_on(hs_color=(120, 100))),
            asyncio.create_task(light.async_turn_on(brightness=200)),
            asyncio.create_task(light.async_turn_off()),
            asyncio.create_task(light.async_turn_on(color_temp_kelvin=4000)),
        ]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, *pending)
    calls = controller.send.await_args_list
    assert len(calls) == 4
    assert calls[1].args == (hsi_payload(120, 100, round(200 * 1000 / 255)),)
    assert calls[2].args == (protocol.sleep(False),)
    assert calls[3].args == (protocol.sleep(True), protocol.cct(round(200 * 1000 / 255), 400))
    assert light.is_on is True
    assert light.brightness == 200
    assert light.color_temp_kelvin == 4000


@pytest.mark.asyncio
async def test_coalesced_callers_receive_send_failure():
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(*_payloads):
        if not started.is_set():
            started.set()
            await release.wait()
            return
        raise HomeAssistantError("Bluetooth failed")

    controller = SimpleNamespace(send=AsyncMock(side_effect=send))
    entry = SimpleNamespace(unique_id="AA:BB:CC:DD:EE:FF", runtime_data=controller, title="Test")
    light = B7CLight(entry)
    with patch.object(light, "async_write_ha_state"):
        first = asyncio.create_task(light.async_turn_on(brightness=50))
        await started.wait()
        pending = [
            asyncio.create_task(light.async_turn_on(brightness=100)),
            asyncio.create_task(light.async_turn_on(brightness=200)),
        ]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, *pending, return_exceptions=True)
    assert results[0] is None
    assert all(isinstance(result, HomeAssistantError) for result in results[1:])
    assert controller.send.await_count == 2
    assert light.brightness == 50
