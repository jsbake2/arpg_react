"""D4 in-game loot filter — base64+protobuf decoder/encoder.

Round-trips D4's clipboard import/export string format. No external proto
runtime needed; this implements just enough of protobuf wire format to
parse and emit the message shape we reverse-engineered from the spike
samples in `D4_FILTERS/`.

Schema sketched in D4_FILTER_SCHEMA.md.

Usage:
    decode_string(s) -> dict       # base64 string from D4's Export → nested dict
    encode_filter(d) -> str        # nested dict → base64 string D4's Import accepts
    pretty(d) -> str               # pretty-print a decoded filter for debugging

CLI:
    python tools/d4_filter_codec.py decode D4_FILTERS/SPIKE_C_MULTI_COND
    python tools/d4_filter_codec.py decode-all D4_FILTERS/
"""

from __future__ import annotations

import base64
import struct
import sys
from io import BytesIO
from pathlib import Path


# --- low-level wire format -------------------------------------------------

def _read_varint(buf: BytesIO) -> int:
    result, shift = 0, 0
    while True:
        b = buf.read(1)
        if not b:
            raise EOFError("truncated varint")
        b = b[0]
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            return result
        shift += 7


def _write_varint(out: bytearray, n: int) -> None:
    while n > 0x7f:
        out.append((n & 0x7f) | 0x80)
        n >>= 7
    out.append(n & 0x7f)


def _decode_message(data: bytes) -> list[tuple[int, str, object]]:
    """Decode a protobuf message into a list of (field, kind, value) tuples.

    `kind` is one of: 'varint', 'fixed32', 'fixed64', 'msg', 'bytes', 'str'.
    Heuristically tries to descend into length-delimited fields as nested
    messages; falls back to bytes / utf-8 string when that fails.
    """
    buf = BytesIO(data)
    out: list[tuple[int, str, object]] = []
    while buf.tell() < len(data):
        tag = _read_varint(buf)
        field = tag >> 3
        wt = tag & 0x7
        if wt == 0:
            out.append((field, "varint", _read_varint(buf)))
        elif wt == 1:
            out.append((field, "fixed64", struct.unpack("<Q", buf.read(8))[0]))
        elif wt == 5:
            out.append((field, "fixed32", struct.unpack("<I", buf.read(4))[0]))
        elif wt == 2:
            ln = _read_varint(buf)
            payload = buf.read(ln)
            try:
                nested = _decode_message(payload)
                if nested:
                    out.append((field, "msg", nested))
                    continue
            except Exception:
                pass
            try:
                s = payload.decode("ascii")
                if s.isprintable():
                    out.append((field, "str", s))
                    continue
            except UnicodeDecodeError:
                pass
            out.append((field, "bytes", payload))
        else:
            raise ValueError(f"unknown wire type {wt} at offset {buf.tell()}")
    return out


def _encode_field(out: bytearray, field: int, kind: str, value) -> None:
    """Append one field to the buffer."""
    if kind == "varint":
        _write_varint(out, (field << 3) | 0)
        _write_varint(out, value)
    elif kind == "fixed32":
        _write_varint(out, (field << 3) | 5)
        out.extend(struct.pack("<I", value))
    elif kind == "fixed64":
        _write_varint(out, (field << 3) | 1)
        out.extend(struct.pack("<Q", value))
    elif kind == "str":
        payload = value.encode("utf-8")
        _write_varint(out, (field << 3) | 2)
        _write_varint(out, len(payload))
        out.extend(payload)
    elif kind == "bytes":
        _write_varint(out, (field << 3) | 2)
        _write_varint(out, len(value))
        out.extend(value)
    elif kind == "msg":
        payload = _encode_message(value)
        _write_varint(out, (field << 3) | 2)
        _write_varint(out, len(payload))
        out.extend(payload)
    else:
        raise ValueError(f"unsupported kind {kind!r}")


def _encode_message(items: list[tuple[int, str, object]]) -> bytes:
    out = bytearray()
    for field, kind, value in items:
        _encode_field(out, field, kind, value)
    return bytes(out)


# --- public API ------------------------------------------------------------

def decode_string(s: str) -> list[tuple[int, str, object]]:
    """Decode a D4-clipboard filter string into the wire-format tree."""
    s = s.strip()
    raw = base64.b64decode(s + "=" * (-len(s) % 4))
    return _decode_message(raw)


def encode_filter(items: list[tuple[int, str, object]]) -> str:
    """Encode a wire-format tree back into a D4-clipboard string."""
    return base64.b64encode(_encode_message(items)).decode("ascii")


def pretty(items, indent: int = 0) -> str:
    """Human-readable dump of a decoded filter."""
    lines = []
    for f, kind, val in items:
        prefix = "  " * indent
        if kind == "msg":
            lines.append(f"{prefix}field {f} (msg):")
            lines.append(pretty(val, indent + 1))
        elif kind == "fixed32":
            lines.append(f"{prefix}field {f} (fixed32): {val} = 0x{val:08x}")
        elif kind == "fixed64":
            lines.append(f"{prefix}field {f} (fixed64): {val} = 0x{val:016x}")
        elif kind == "bytes":
            lines.append(f"{prefix}field {f} (bytes): {val.hex()}")
        else:
            lines.append(f"{prefix}field {f} ({kind}): {val!r}")
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    cmd = argv[0]

    if cmd == "decode":
        if len(argv) < 2:
            print("usage: decode <file>", file=sys.stderr)
            return 2
        s = Path(argv[1]).read_text().strip()
        decoded = decode_string(s)
        print(pretty(decoded))
        # Round-trip check.
        re_encoded = encode_filter(decoded)
        match = re_encoded == s
        print(f"\nround-trip: {'OK' if match else 'MISMATCH'}")
        if not match:
            print(f"  original:   {s}")
            print(f"  re-encoded: {re_encoded}")
        return 0 if match else 1

    if cmd == "decode-all":
        if len(argv) < 2:
            print("usage: decode-all <dir>", file=sys.stderr)
            return 2
        d = Path(argv[1])
        ok = True
        for f in sorted(d.iterdir()):
            if not f.is_file():
                continue
            print(f"\n=== {f.name} ===")
            s = f.read_text().strip()
            decoded = decode_string(s)
            print(pretty(decoded))
            re_encoded = encode_filter(decoded)
            match = re_encoded == s
            print(f"round-trip: {'OK' if match else 'MISMATCH'}")
            if not match:
                ok = False
        return 0 if ok else 1

    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
