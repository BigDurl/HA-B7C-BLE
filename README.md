# Aputure B7c BLE for Home Assistant

Bluetooth control for Aputure Accent B7c bulbs: on/off,
brightness, white temperature and hue/saturation. No cloud or phone is needed.

## Requirements

- Home Assistant 2026.10.4 or later with an available Bluetooth adapter or an
  active-connection Bluetooth proxy in range of each bulb.
- A factory-reset B7c. Provisioning **removes it from its previous mesh**,
  including Sidus Link or standalone controller. Each bulb must be reset
  and added separately; this integration cannot import existing mesh keys.

## Marketplace installation

1. In Home Assistant, go to **Settings → Marketplace → Add from link**.
2. Connect GitHub if requested and add
   `https://github.com/BigDurl/HA-B7C-BLE` as an integration.
3. Install it and restart Home Assistant if prompted.
4. Factory-reset one B7c, then use **Settings → Devices & services → Add
   integration → Aputure B7c BLE**. Select the bulb and confirm provisioning.
5. Repeat for each additional reset bulb.

The Marketplace's **Add from link** supports custom public repositories; this
does not mean the project is listed in the Marketplace catalog.

## Safety and limitations

Each bulb gets a separate, randomly generated mesh network. Consequently,
Home Assistant needs a direct Bluetooth connection to each bulb; this
integration does not provide mesh group addressing or proxy relaying.
Commands are unacknowledged, and the entity state is optimistic. If the
bulb is changed outside Home Assistant, the displayed state may be stale.

The private `.storage/b7c_ble_<address>` file in Home Assistant's config
directory holds the network, application, and device keys and sequence
numbers. Do not share it. The device key is saved before provisioning data
is sent, and sequence numbers are durably reserved before use. If setup
reports uncertain provisioning, submit the setup form again: the saved key
is reused and Home Assistant attempts configuration without resetting.

Do not run two Home Assistant instances with copies of the same state, or
restore an older backup and continue using the same mesh: sequence-number
reuse can cause replay rejection or cryptographic nonce reuse. After a restore
or lost state, factory-reset each affected bulb and add it as a new mesh.
Removing an integration entry does not delete its mesh state. To deliberately
start over, **first factory-reset the bulb**, remove its integration entry,
then delete only that bulb's matching `.storage/b7c_ble_<address>` file and
add it again. Removing keys without resetting can strand the bulb. To return
to Sidus Link, factory-reset it and add it there.

Bluetooth connection failures are surfaced to Home Assistant. This initial
version opens a direct connection per operation and may be slow or unreliable
on adapters with intermittent connection timeouts.

## Development

The MIT-licensed mesh stack and protocol helpers are adapted from
[HA-Amaran-BLE](https://github.com/nhazdun/HA-Amaran-BLE), commit
`ed4c5921ac10db8953145ef6ec8806113aabaf41`; see [LICENSE](LICENSE).
Provisioning was changed to persist the device key before sending provisioning
data. B7c color payloads were verified with the independent standalone
controller, not this integration.

The focused test suite uses the Home Assistant test harness:

```sh
python -m pip install pytest-homeassistant-custom-component==0.13.375 \
  aiousbwatcher==1.1.2 serialx==1.11.0
python -m pytest -q tests
```
