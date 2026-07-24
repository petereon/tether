"""Wire-compatibility check between the PC-side `msgpack` package (chunk 2)
and the vendored MicroPython `tether_runtime.umsgpack` port (chunk 5). Not a
TDD exercise in the usual sense - this is integration verification of a
vendored third-party component, not new logic of ours.
"""

import msgpack
import pytest

from tether_runtime import umsgpack

ROUNDTRIP_VALUES = [
    None,
    True,
    False,
    0,
    -1,
    42,
    -1_000_000,
    2**40,
    3.5,
    "hello",
    "unicode: éèü",
    b"\x00\x01\xff",
    [1, 2, 3],
    {"a": 1, "b": [1, 2]},
    {"nested": {"labels": ["x", "y"], "count": None}},
    # Force the longer-form wire encodings (fixstr/fixarray/fixmap/fixbin
    # only cover small sizes) - a length-field byte-order bug in the
    # vendored codec's str8/16, array16, map16, or bin16 branches wouldn't
    # be caught by small values alone.
    "x" * 40,  # > 31 bytes: past fixstr, into str8
    b"\x00" * 300,  # > 255 bytes: past bin8, into bin16
    list(range(20)),  # > 15 elements: past fixarray, into array16
    {f"k{i}": i for i in range(20)},  # > 15 pairs: past fixmap, into map16
]


@pytest.mark.parametrize("value", ROUNDTRIP_VALUES)
def test_pc_msgpack_encodes_umsgpack_decodes(value):
    encoded = msgpack.packb(value, use_bin_type=True)
    assert umsgpack.loads(encoded) == value


@pytest.mark.parametrize("value", ROUNDTRIP_VALUES)
def test_umsgpack_encodes_pc_msgpack_decodes(value):
    encoded = umsgpack.dumps(value)
    assert msgpack.unpackb(encoded, raw=False) == value


def test_bytes_and_str_are_distinguishable_after_roundtrip():
    # The core reason DESIGN.md picked msgpack over JSON: bytes must not
    # become a str (or vice versa) crossing the wire in either direction.
    encoded_by_pc = msgpack.packb(b"raw", use_bin_type=True)
    decoded_by_mcu = umsgpack.loads(encoded_by_pc)
    assert isinstance(decoded_by_mcu, bytes)

    encoded_by_mcu = umsgpack.dumps("text")
    decoded_by_pc = msgpack.unpackb(encoded_by_mcu, raw=False)
    assert isinstance(decoded_by_pc, str)
