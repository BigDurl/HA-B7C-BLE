# Changelog

## 1.0.0

Initial release of the Aputure Accent B7C Home Assistant integration.

- Provision factory-reset B7C bulbs over Bluetooth without a cloud account.
- Control power, brightness, white temperature, and hue/saturation.
- Reuse connections during adjustments and restore saved light settings after
  Home Assistant restarts.
- Use the Bluetooth characteristic's write limit for mesh packet segmentation.

Bulbs must be reset before provisioning. Each bulb uses its own mesh and needs
a direct Bluetooth connection; light commands are unacknowledged. See the
[README](README.md) for recovery, backup safety, and other limitations.
