"""The slice of the Nitro Security Module API this system uses, as CBOR over a stream.

Requests and responses keep the shape of aws-nitro-enclaves-nsm-api, so a real
/dev/nsm client can replace the socket client without touching callers.
"""

import socket
import struct
from typing import Any, Final

from protocol import cbor

MAX_FRAME_BYTES: Final = 16 * 1024
MAX_NONCE_BYTES: Final = 512
MAX_USER_DATA_BYTES: Final = 512
MAX_PUBLIC_KEY_BYTES: Final = 1024
_LENGTH = struct.Struct(">I")


class NsmError(RuntimeError):
    pass


def attestation_request(*, public_key: bytes, user_data: bytes, nonce: bytes) -> dict[str, Any]:
    return {"Attestation": {"public_key": public_key, "user_data": user_data, "nonce": nonce}}


def describe_pcr_request(index: int) -> dict[str, Any]:
    return {"DescribePCR": {"index": index}}


def send(sock: socket.socket, message: dict[str, Any]) -> None:
    payload = cbor.encode(message)
    if len(payload) > MAX_FRAME_BYTES:
        raise NsmError("frame too large")
    sock.sendall(_LENGTH.pack(len(payload)) + payload)


def receive(sock: socket.socket) -> dict[str, Any]:
    (length,) = _LENGTH.unpack(_read_exactly(sock, _LENGTH.size))
    if length > MAX_FRAME_BYTES:
        raise NsmError("frame too large")
    return cbor.decode_map(_read_exactly(sock, length))


def _read_exactly(sock: socket.socket, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise NsmError("connection closed mid-frame")
        buffer += chunk
    return bytes(buffer)
