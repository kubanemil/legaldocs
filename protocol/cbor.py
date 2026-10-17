"""Deterministic CBOR encoding and strict decoding."""

from io import BytesIO
from typing import Any

import cbor2


class DecodeError(ValueError):
    pass


def encode(value: Any) -> bytes:
    return cbor2.dumps(value, canonical=True)


def decode(data: bytes, *, max_depth: int = 16) -> Any:
    stream = BytesIO(data)
    try:
        value = cbor2.CBORDecoder(
            stream, max_depth=max_depth, allow_duplicate_keys=False, allow_indefinite=False
        ).decode()
    except (cbor2.CBORDecodeError, RecursionError) as exc:
        raise DecodeError("malformed CBOR") from exc
    if stream.tell() != len(data):
        raise DecodeError("trailing bytes after CBOR value")
    return value


def decode_map(data: bytes) -> dict[Any, Any]:
    value = decode(data)
    if not isinstance(value, dict):
        raise DecodeError("expected a CBOR map")
    return value


def field[T](mapping: dict[Any, Any], key: Any, kind: type[T]) -> T:
    value = mapping.get(key)
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise DecodeError(f"field {key!r} must be {kind.__name__}")
    return value


def optional_field[T](mapping: dict[Any, Any], key: Any, kind: type[T]) -> T | None:
    if mapping.get(key) is None:
        return None
    return field(mapping, key, kind)
