import os
from dataclasses import dataclass
from pathlib import Path

from protocol.attestation import PCR_BYTES


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    enclave_host: str
    enclave_port: int
    parent_cert: Path
    parent_key: Path
    nsm_trust_anchor: Path
    expected_pcr0: str
    eth_rpc_url: str
    registry_address: str
    relayer_key: str
    max_envelope_bytes: int

    def __post_init__(self) -> None:
        if len(bytes.fromhex(self.expected_pcr0)) != PCR_BYTES:
            raise ValueError("EXPECTED_PCR0 must be the enclave's 48-byte PCR0 in hex (./do pcr0)")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            database_url=env["DATABASE_URL"],
            redis_url=env["REDIS_URL"],
            enclave_host=env.get("ENCLAVE_HOST", "enclave"),
            enclave_port=int(env.get("ENCLAVE_PORT", "8443")),
            parent_cert=Path(env["PARENT_CERT"]),
            parent_key=Path(env["PARENT_KEY"]),
            nsm_trust_anchor=Path(env["NSM_TRUST_ANCHOR"]),
            expected_pcr0=env["EXPECTED_PCR0"],
            eth_rpc_url=env.get("ETH_RPC_URL", ""),
            registry_address=env.get("REGISTRY_ADDRESS", ""),
            relayer_key=env.get("RELAYER_KEY", ""),
            max_envelope_bytes=int(env.get("MAX_ENVELOPE_BYTES", str(8 * 1024 * 1024))),
        )
