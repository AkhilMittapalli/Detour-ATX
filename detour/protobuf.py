"""A minimal protobuf wire-format reader.

CapMetro publishes GTFS-Realtime as protobuf and nothing else — the JSON
mirrors on the state portal return 403. The official way to read it is the
`gtfs-realtime-bindings` package, which pulls in `protobuf`, which would be
the first dependency in this project.

The wire format is small enough not to need one. Every field is a varint key
carrying a field number and a wire type, followed by a payload whose shape
the wire type determines. That is four cases, and none of them need a schema:

    0  varint          ints, bools, enums
    1  64-bit          double, fixed64
    2  length-delimited  strings, bytes, nested messages, packed repeated
    5  32-bit          float, fixed32

So this decodes into nested dicts keyed by field number, and `transit.py`
maps the numbers it cares about onto names. Being schema-free means a change
to the GTFS-RT spec cannot break the parse, only the mapping.

Wire type 2 is ambiguous by design — a nested message and a string look
identical on the wire. The reader tries a nested parse and falls back to
bytes, which is what every protobuf debugger does.
"""

from __future__ import annotations

import struct
from typing import Any

MAX_DEPTH = 12


class ProtobufError(ValueError):
    pass


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Base-128, little-endian, high bit as the continuation flag."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ProtobufError("truncated varint")
        if shift > 63:
            raise ProtobufError("varint too long")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _looks_like_text(raw: bytes) -> bool:
    if not raw:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(c == "\n" or c == "\t" or c >= " " for c in text)


def parse(data: bytes, *, depth: int = 0) -> dict[int, list[Any]]:
    """Decode one message into {field_number: [values]}.

    Values are int, float, str, bytes, or a nested dict. Repeated fields come
    back as multiple list entries, which is why every value is a list — in
    protobuf any field may legally repeat.
    """
    if depth > MAX_DEPTH:
        raise ProtobufError("message nested too deeply")

    fields: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        field_number, wire_type = key >> 3, key & 0x07
        if field_number == 0:
            raise ProtobufError("field number 0 is invalid")

        if wire_type == 0:
            value, pos = read_varint(data, pos)
        elif wire_type == 1:
            if pos + 8 > len(data):
                raise ProtobufError("truncated 64-bit field")
            value = struct.unpack_from("<d", data, pos)[0]
            pos += 8
        elif wire_type == 5:
            if pos + 4 > len(data):
                raise ProtobufError("truncated 32-bit field")
            value = struct.unpack_from("<f", data, pos)[0]
            pos += 4
        elif wire_type == 2:
            length, pos = read_varint(data, pos)
            if pos + length > len(data):
                raise ProtobufError("truncated length-delimited field")
            raw = data[pos : pos + length]
            pos += length
            value = _interpret(raw, depth)
        else:
            raise ProtobufError(f"unsupported wire type {wire_type}")

        fields.setdefault(field_number, []).append(value)
    return fields


def _interpret(raw: bytes, depth: int):
    """Resolve the ambiguity in wire type 2.

    A nested message, a UTF-8 string and a byte blob are indistinguishable on
    the wire. Try the structured reading first, because a mis-read string is
    harmless while a mis-read message loses data.
    """
    if not raw:
        return ""
    try:
        nested = parse(raw, depth=depth + 1)
        if nested:
            return nested
    except ProtobufError:
        pass
    return raw.decode("utf-8") if _looks_like_text(raw) else raw


# --------------------------------------------------------------------------
# Small helpers for walking the result
# --------------------------------------------------------------------------

def first(fields: dict[int, list[Any]] | None, *path: int):
    """Follow a field-number path, taking the first value at each step."""
    node: Any = fields
    for number in path:
        if not isinstance(node, dict):
            return None
        values = node.get(number)
        if not values:
            return None
        node = values[0]
    return node


def every(fields: dict[int, list[Any]] | None, number: int) -> list[Any]:
    if not isinstance(fields, dict):
        return []
    return fields.get(number, [])


def as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)
