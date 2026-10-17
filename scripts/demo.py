"""Run two tenants' contracts through the running stack and show what each party sees.

Start the stack with `./do dev`, then `./do demo`.
"""

import os
from pathlib import Path

import httpx

from client.sdk import LegalDocsClient, Result, anchored_at
from nsm.measure import measure
from protocol.attestation import Policy

ROOT = Path(__file__).resolve().parent.parent

SAMPLES = {
    "acme": [
        "MERGER AGREEMENT\n\nThis Merger Agreement is entered into as of March 15, 2025, by and "
        "between Acme Corporation, a Delaware corporation, and Pinnacle Industries LLC, a Nevada "
        "limited liability company.\n\nThe effective date of this Merger shall be April 1, 2025.",
        "NON-DISCLOSURE AGREEMENT\n\nThis NDA is made effective as of January 10, 2025, by and "
        "between Acme Corporation and TechVentures Inc.\n\nThis NDA remains in effect until "
        "December 31, 2026.",
    ],
    "globex": [
        "SOFTWARE LICENSE AGREEMENT\n\nThis Agreement is entered into as of February 20, 2025, by "
        "and between Globex Corporation, a California corporation, and DataStream Analytics Ltd., "
        "a UK limited company.\n\nThe term runs from 2025-03-01 to 02/28/2028.",
    ],
}


def policy() -> Policy:
    anchor = Path(os.environ.get("NSM_TRUST_ANCHOR", ROOT / "trust/nsm-root.pem")).read_bytes()
    return Policy.from_pem(anchor, os.environ.get("EXPECTED_PCR0") or measure(ROOT)[0].hex())


def connect(api_key: str, expected_pcr0: bytes | None = None) -> LegalDocsClient:
    trusted = policy()
    return LegalDocsClient(
        os.environ.get("API_URL", "http://localhost:8000"),
        api_key,
        trust_anchor=trusted.trust_anchor,
        expected_pcr0=expected_pcr0 or trusted.expected_pcr0,
    )


def on_chain(result: Result) -> int:
    return anchored_at(
        os.environ.get("ETH_RPC_URL", "http://localhost:8545"),
        os.environ["REGISTRY_ADDRESS"],
        result.receipt_hash,
    )


def main() -> None:
    keys = dict(pair.split("=", 1) for pair in os.environ["DEMO_KEYS"].split(","))
    results: dict[str, list[Result]] = {}
    for tenant, documents in SAMPLES.items():
        client = connect(keys[tenant])
        identity = client.attest()
        print(f"\n{tenant}: attested enclave {identity.key_id.hex()}, PCR0 {identity.pcr0.hex()}")
        results[tenant] = []
        for text in documents:
            submission = client.submit(text.encode(), identity)
            result = client.wait(submission, anchored=True)
            results[tenant].append(result)
            print(f"  document {result.document_id}")
            print(f"    parties  {', '.join(result.fields['parties'])}")
            print(f"    dates    {', '.join(result.fields['dates'])}")
            print(f"    receipt  {result.receipt_hash.hex()} anchored at {on_chain(result)}")

    acme_document = results["acme"][0].document_id
    try:
        connect(keys["globex"]).status(acme_document)
    except httpx.HTTPStatusError as exc:
        print(
            f"\nglobex asking for acme's document {acme_document}: HTTP {exc.response.status_code}"
        )
    else:
        raise SystemExit("globex could read acme's document")


if __name__ == "__main__":
    main()
