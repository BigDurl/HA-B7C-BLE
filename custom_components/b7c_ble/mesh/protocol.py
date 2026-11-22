"""Aputure light payloads carried by mesh opcode 0x26."""

VENDOR_OPCODE = 0x26


def pack(fields):
    """Pack little-endian bit fields and calculate the first-byte checksum."""
    value = 0
    offset = 0
    for raw, width in fields:
        if width <= 0 or not 0 <= raw < 1 << width:
            raise ValueError(f"value {raw} does not fit in {width} bits")
        value |= raw << offset
        offset += width
    if offset != 80:
        raise ValueError(f"payload is {offset} bits, expected 80")
    data = bytearray(value.to_bytes(10, "little"))
    data[0] = sum(data[1:]) & 0xFF
    return bytes(data)


def sleep(on):
    return pack([(0, 8), (0, 20), (0, 20), (0, 16), (int(on), 8), (12, 7), (1, 1)])


def cct(intensity, cct_deci_kelvin):
    """Build a neutral green/magenta CCT message."""
    if not 200 <= cct_deci_kelvin <= 1000:
        raise ValueError("B7C CCT must be 2000 to 10000 K")
    return pack([
        (0, 8), (1, 1), (0, 20), (0, 13), (0, 1), (0, 1), (0, 1),
        (10, 7), (cct_deci_kelvin, 10), (intensity, 10), (2, 7), (1, 1),
    ])
