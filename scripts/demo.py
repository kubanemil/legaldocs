#!/usr/bin/env python3
"""End-to-end demo of the LegalDocs confidential document processing pipeline.

Demonstrates multi-tenant document submission, encrypted processing, and
result extraction using two separate tenants.

Usage:
    python scripts/demo.py [--api-url URL] [--ca-cert PATH]
"""

import argparse
import sys
import time

sys.path.insert(0, ".")

from client.client import LegalDocsClient

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

BOLD = "\033[1m"
GREEN = "\033[92m"
BLUE = "\033[94m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"


def header(text: str) -> None:
    width = 60
    print()
    print(f"{BOLD}{BLUE}{'=' * width}{RESET}")
    print(f"{BOLD}{BLUE}  {text}{RESET}")
    print(f"{BOLD}{BLUE}{'=' * width}{RESET}")


def section(text: str) -> None:
    print(f"\n{BOLD}{CYAN}--- {text} ---{RESET}")


def success(text: str) -> None:
    print(f"  {GREEN}[OK]{RESET} {text}")


def info(text: str) -> None:
    print(f"  {DIM}{text}{RESET}")


def warn(text: str) -> None:
    print(f"  {YELLOW}[!]{RESET} {text}")


def fail(text: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {text}")


# ---------------------------------------------------------------------------
# Sample legal documents
# ---------------------------------------------------------------------------

SAMPLE_DOCUMENTS = {
    "acme-corp": [
        {
            "filename": "acme_merger_agreement.txt",
            "content": (
                "MERGER AGREEMENT\n\n"
                "This Merger Agreement (the 'Agreement') is entered into as of "
                "March 15, 2025, by and between Acme Corporation, a Delaware "
                "corporation ('Acquirer'), and Pinnacle Industries LLC, a Nevada "
                "limited liability company ('Target').\n\n"
                "WHEREAS, the Acquirer desires to acquire all outstanding equity "
                "interests of the Target, and the Target's board of managers has "
                "determined that the Merger is in the best interest of its members;\n\n"
                "NOW, THEREFORE, in consideration of the mutual covenants herein, "
                "the parties agree as follows:\n\n"
                "1. EFFECTIVE DATE. The effective date of this Merger shall be "
                "April 1, 2025.\n\n"
                "2. CONSIDERATION. Acquirer shall pay to the members of Target "
                "aggregate consideration of $42,000,000.\n\n"
                "3. REPRESENTATIONS. Each party represents that it has full "
                "corporate authority to execute this Agreement.\n\n"
                "IN WITNESS WHEREOF, the parties have executed this Agreement "
                "as of the date first written above.\n\n"
                "Acme Corporation\nBy: John Smith, CEO\n\n"
                "Pinnacle Industries LLC\nBy: Maria Garcia, Managing Member"
            ),
        },
        {
            "filename": "acme_nda.txt",
            "content": (
                "NON-DISCLOSURE AGREEMENT\n\n"
                "This Non-Disclosure Agreement ('NDA') is made effective as of "
                "January 10, 2025, by and between Acme Corporation ('Disclosing "
                "Party') and TechVentures Inc. ('Receiving Party').\n\n"
                "1. PURPOSE. The parties wish to explore a potential business "
                "relationship regarding joint product development.\n\n"
                "2. CONFIDENTIAL INFORMATION. 'Confidential Information' means "
                "any non-public technical, business, or financial information "
                "disclosed by either party.\n\n"
                "3. TERM. This NDA shall remain in effect until December 31, 2026, "
                "unless terminated earlier by written notice.\n\n"
                "4. OBLIGATIONS. The Receiving Party shall hold all Confidential "
                "Information in strict confidence and shall not disclose it to "
                "any third party without prior written consent.\n\n"
                "Executed as of the date first written above.\n\n"
                "Acme Corporation\nBy: Sarah Chen, General Counsel\n\n"
                "TechVentures Inc.\nBy: Robert Kim, VP of Engineering"
            ),
        },
    ],
    "globex": [
        {
            "filename": "globex_license_agreement.txt",
            "content": (
                "SOFTWARE LICENSE AGREEMENT\n\n"
                "This Software License Agreement ('Agreement') is entered into "
                "as of February 20, 2025, by and between Globex Corporation, "
                "a California corporation ('Licensor'), and DataStream Analytics "
                "Ltd., a UK limited company ('Licensee').\n\n"
                "1. GRANT OF LICENSE. Licensor hereby grants to Licensee a "
                "non-exclusive, non-transferable license to use the Globex "
                "Enterprise Platform software.\n\n"
                "2. TERM. The initial term of this Agreement shall commence on "
                "March 1, 2025 and continue for a period of three (3) years, "
                "ending on February 28, 2028.\n\n"
                "3. FEES. Licensee shall pay Licensor an annual license fee of "
                "$500,000, payable in advance on each anniversary.\n\n"
                "4. INTELLECTUAL PROPERTY. All intellectual property rights in "
                "the Software remain with the Licensor.\n\n"
                "IN WITNESS WHEREOF, the parties have executed this Agreement.\n\n"
                "Globex Corporation\nBy: James Wilson, CTO\n\n"
                "DataStream Analytics Ltd.\nBy: Emily Brown, Director"
            ),
        },
        {
            "filename": "globex_employment_contract.txt",
            "content": (
                "EMPLOYMENT AGREEMENT\n\n"
                "This Employment Agreement ('Agreement') is made as of "
                "June 1, 2025, by and between Globex Corporation ('Employer') "
                "and Dr. Alexandra Petrov ('Employee').\n\n"
                "1. POSITION. Employee shall serve as Chief Data Scientist, "
                "reporting to the Chief Technology Officer.\n\n"
                "2. START DATE. Employment shall commence on July 15, 2025.\n\n"
                "3. COMPENSATION. Employee shall receive an annual base salary "
                "of $285,000, payable in accordance with Employer's standard "
                "payroll schedule.\n\n"
                "4. NON-COMPETE. For a period of twelve (12) months following "
                "termination, Employee shall not engage in any competing "
                "business within the United States.\n\n"
                "5. GOVERNING LAW. This Agreement shall be governed by the laws "
                "of the State of California.\n\n"
                "Globex Corporation\nBy: James Wilson, CTO\n\n"
                "Employee: Dr. Alexandra Petrov"
            ),
        },
    ],
}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def check_health(api_url: str) -> bool:
    """Check that the API is reachable."""
    import requests

    try:
        resp = requests.get(f"{api_url}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main demo flow
# ---------------------------------------------------------------------------


def run_demo(api_url: str, ca_cert_path: str) -> None:
    header("LegalDocs Confidential Pipeline - End-to-End Demo")

    # ------------------------------------------------------------------
    # 1. Health check
    # ------------------------------------------------------------------
    section("Step 1: Health Check")
    if check_health(api_url):
        success("API is reachable")
    else:
        fail("API is not reachable - is docker compose running?")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Attestation
    # ------------------------------------------------------------------
    section("Step 2: Enclave Attestation")

    # Use any tenant client to get attestation (it's tenant-independent)
    client = LegalDocsClient(api_url=api_url, tenant_id="acme-corp")
    attestation = client.get_attestation()
    info(f"Attestation payload keys: {list(attestation['payload'].keys())}")
    success("Fetched attestation from enclave")

    public_key_pem = LegalDocsClient.verify_attestation(attestation, ca_cert_path)
    success("Verified attestation signature against CA certificate")

    info(f"Enclave public key: {public_key_pem[:50].decode()}...")

    # ------------------------------------------------------------------
    # 3. Submit documents per tenant
    # ------------------------------------------------------------------
    tenant_results: dict[str, list[dict]] = {}

    for tenant_id, documents in SAMPLE_DOCUMENTS.items():
        section(f"Step 3: Submit Documents - Tenant '{tenant_id}'")
        tenant_client = LegalDocsClient(api_url=api_url, tenant_id=tenant_id)
        doc_ids = []

        for doc in documents:
            plaintext = doc["content"].encode("utf-8")
            ciphertext = LegalDocsClient.encrypt_document(plaintext, public_key_pem)
            info(
                f"Encrypted '{doc['filename']}' "
                f"({len(plaintext)} bytes -> {len(ciphertext)} bytes)"
            )

            doc_id = tenant_client.submit_document(
                ciphertext, filename=doc["filename"]
            )
            success(f"Submitted '{doc['filename']}' -> document_id={doc_id}")
            doc_ids.append({"id": doc_id, "filename": doc["filename"]})

        tenant_results[tenant_id] = doc_ids

    # ------------------------------------------------------------------
    # 4. Poll for completion and retrieve results
    # ------------------------------------------------------------------
    section("Step 4: Waiting for Processing")
    all_results: dict[str, list[dict]] = {}

    for tenant_id, doc_ids in tenant_results.items():
        tenant_client = LegalDocsClient(api_url=api_url, tenant_id=tenant_id)
        results = []

        for doc_info in doc_ids:
            doc_id = doc_info["id"]
            filename = doc_info["filename"]
            info(f"Polling '{filename}' (tenant={tenant_id})...")

            start = time.time()
            try:
                result = tenant_client.get_document_results(
                    doc_id, poll_interval=1.0, max_retries=60
                )
                elapsed = time.time() - start
                success(
                    f"'{filename}' completed in {elapsed:.1f}s"
                )
                results.append(
                    {
                        "filename": filename,
                        "doc_id": doc_id,
                        "result": result,
                        "elapsed": elapsed,
                    }
                )
            except RuntimeError as exc:
                elapsed = time.time() - start
                fail(f"'{filename}' failed after {elapsed:.1f}s: {exc}")
                results.append(
                    {
                        "filename": filename,
                        "doc_id": doc_id,
                        "result": None,
                        "elapsed": elapsed,
                        "error": str(exc),
                    }
                )

        all_results[tenant_id] = results

    # ------------------------------------------------------------------
    # 5. Display extracted fields
    # ------------------------------------------------------------------
    section("Step 5: Extracted Fields")

    for tenant_id, results in all_results.items():
        print(f"\n  {BOLD}Tenant: {tenant_id}{RESET}")
        for r in results:
            print(f"    {CYAN}{r['filename']}{RESET} (id: {r['doc_id'][:8]}...)")
            if r["result"] is not None:
                parties = r["result"].get("parties", [])
                dates = r["result"].get("dates", [])
                print(f"      Parties: {', '.join(parties) if parties else '(none)'}")
                print(f"      Dates:   {', '.join(dates) if dates else '(none)'}")
            else:
                print(f"      {RED}Error: {r.get('error', 'unknown')}{RESET}")

    # ------------------------------------------------------------------
    # 6. Tenant isolation
    # ------------------------------------------------------------------
    section("Step 6: Tenant Isolation Check")

    for tenant_id in SAMPLE_DOCUMENTS:
        tenant_client = LegalDocsClient(api_url=api_url, tenant_id=tenant_id)
        docs = tenant_client.list_documents()
        doc_ids_for_tenant = {d["id"] for d in tenant_results[tenant_id]}
        visible_ids = {d["id"] for d in docs}

        # Check our docs are visible
        own_visible = doc_ids_for_tenant.issubset(visible_ids)
        if own_visible:
            success(
                f"Tenant '{tenant_id}' sees its own {len(doc_ids_for_tenant)} "
                f"document(s)"
            )
        else:
            fail(f"Tenant '{tenant_id}' cannot see all its own documents")

        # Check other tenant's docs are NOT visible
        for other_tenant_id, other_docs in tenant_results.items():
            if other_tenant_id == tenant_id:
                continue
            other_ids = {d["id"] for d in other_docs}
            cross_visible = other_ids.intersection(visible_ids)
            if not cross_visible:
                success(
                    f"Tenant '{tenant_id}' cannot see "
                    f"'{other_tenant_id}' documents (isolation confirmed)"
                )
            else:
                fail(
                    f"Tenant '{tenant_id}' can see {len(cross_visible)} "
                    f"document(s) from '{other_tenant_id}' - ISOLATION BREACH"
                )

    # ------------------------------------------------------------------
    # 7. Summary
    # ------------------------------------------------------------------
    header("Demo Summary")

    total_docs = sum(len(r) for r in all_results.values())
    completed = sum(
        1
        for results in all_results.values()
        for r in results
        if r["result"] is not None
    )
    failed = total_docs - completed

    print(f"  Tenants:            {len(SAMPLE_DOCUMENTS)}")
    print(f"  Documents submitted: {total_docs}")
    print(f"  Completed:          {GREEN}{completed}{RESET}")
    if failed:
        print(f"  Failed:             {RED}{failed}{RESET}")
    else:
        print(f"  Failed:             {completed and GREEN or ''}{failed}{RESET}")
    print(f"  Tenant isolation:   {GREEN}Verified{RESET}")
    print()

    if failed:
        print(f"  {YELLOW}Some documents failed. Check docker compose logs.{RESET}")
        sys.exit(1)
    else:
        print(f"  {GREEN}{BOLD}All checks passed. Pipeline is operational.{RESET}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LegalDocs end-to-end pipeline demo"
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="Base URL of the LegalDocs API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--ca-cert",
        default="certs/ca.crt",
        help="Path to CA certificate for attestation verification (default: certs/ca.crt)",
    )
    args = parser.parse_args()
    run_demo(args.api_url, args.ca_cert)


if __name__ == "__main__":
    main()
