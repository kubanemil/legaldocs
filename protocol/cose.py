"""COSE_Sign1 (RFC 9052) for the two algorithms this system uses: ES384 and EdDSA."""

from dataclasses import dataclass
from typing import Final

import cbor2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from protocol import cbor

ES384: Final = -35
EDDSA: Final = -8

_HEADER_ALG: Final = 1
_SIGN1_TAG: Final = 18
_P384_SCALAR_BYTES: Final = 48

type SigningKey = ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey
type VerifyingKey = ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey


class CoseError(ValueError):
    pass


@dataclass(frozen=True)
class Sign1:
    protected: bytes
    payload: bytes
    signature: bytes

    @property
    def alg(self) -> int:
        header = cbor.decode_map(self.protected) if self.protected else {}
        alg = header.get(_HEADER_ALG)
        if not isinstance(alg, int):
            raise CoseError("missing alg in protected header")
        return alg

    def verify(self, key: VerifyingKey) -> bytes:
        message = _sig_structure(self.protected, self.payload)
        try:
            if isinstance(key, ec.EllipticCurvePublicKey):
                self._require(ES384, ec.SECP384R1, key)
                if len(self.signature) != 2 * _P384_SCALAR_BYTES:
                    raise CoseError("bad ES384 signature length")
                r = int.from_bytes(self.signature[:_P384_SCALAR_BYTES])
                s = int.from_bytes(self.signature[_P384_SCALAR_BYTES:])
                key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA384()))
            else:
                if self.alg != EDDSA:
                    raise CoseError("expected EdDSA")
                key.verify(self.signature, message)
        except InvalidSignature as exc:
            raise CoseError("signature does not verify") from exc
        return self.payload

    def _require(
        self, alg: int, curve: type[ec.EllipticCurve], key: ec.EllipticCurvePublicKey
    ) -> None:
        if self.alg != alg or not isinstance(key.curve, curve):
            raise CoseError("algorithm does not match key")


def sign(payload: bytes, key: SigningKey) -> bytes:
    if isinstance(key, ec.EllipticCurvePrivateKey):
        if not isinstance(key.curve, ec.SECP384R1):
            raise CoseError("ES384 needs a P-384 key")
        protected = cbor.encode({_HEADER_ALG: ES384})
        der = key.sign(_sig_structure(protected, payload), ec.ECDSA(hashes.SHA384()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(_P384_SCALAR_BYTES) + s.to_bytes(_P384_SCALAR_BYTES)
    else:
        protected = cbor.encode({_HEADER_ALG: EDDSA})
        signature = key.sign(_sig_structure(protected, payload))
    return cbor.encode([protected, {}, payload, signature])


def decode(data: bytes) -> Sign1:
    value = cbor.decode(data)
    if isinstance(value, cbor2.CBORTag):
        if value.tag != _SIGN1_TAG:
            raise CoseError("not a COSE_Sign1 tag")
        value = value.value
    if not isinstance(value, list | tuple) or len(value) != 4:
        raise CoseError("COSE_Sign1 must be a 4-element array")
    protected, unprotected, payload, signature = value
    if not (
        isinstance(protected, bytes)
        and isinstance(unprotected, dict)
        and isinstance(payload, bytes)
        and isinstance(signature, bytes)
    ):
        raise CoseError("malformed COSE_Sign1")
    return Sign1(protected, payload, signature)


def _sig_structure(protected: bytes, payload: bytes) -> bytes:
    return cbor.encode(["Signature1", protected, b"", payload])
