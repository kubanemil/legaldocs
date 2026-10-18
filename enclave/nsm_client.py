"""How the enclave talks to its NSM. Milestone 2 swaps the socket for /dev/nsm."""

import socket
from typing import Any, Protocol

from protocol import nsm_api


class Nsm(Protocol):
    def attest(self, *, public_key: bytes, user_data: bytes, nonce: bytes) -> bytes: ...

    def describe_pcr(self, index: int) -> bytes: ...


class UnixSocketNsm:
    def __init__(self, path: str, timeout: float = 5.0) -> None:
        self._path = path
        self._timeout = timeout

    def attest(self, *, public_key: bytes, user_data: bytes, nonce: bytes) -> bytes:
        request = nsm_api.attestation_request(
            public_key=public_key, user_data=user_data, nonce=nonce
        )
        return _bytes_at(self._call(request), "Attestation", "document")

    def describe_pcr(self, index: int) -> bytes:
        return _bytes_at(self._call(nsm_api.describe_pcr_request(index)), "DescribePCR", "data")

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._timeout)
            sock.connect(self._path)
            nsm_api.send(sock, request)
            response = nsm_api.receive(sock)
        if "Error" in response:
            raise nsm_api.NsmError(f"NSM refused the request: {response['Error']}")
        return response


def _bytes_at(response: dict[str, Any], kind: str, name: str) -> bytes:
    body = response.get(kind)
    value = body.get(name) if isinstance(body, dict) else None
    if not isinstance(value, bytes):
        raise nsm_api.NsmError("malformed NSM response")
    return value
