#!/usr/bin/env python3
"""Compile DocumentRegistry.sol and output ABI + bytecode as JSON."""

import json
import subprocess
import sys
from pathlib import Path

CONTRACTS_DIR = Path(__file__).parent
OUTPUT_FILE = CONTRACTS_DIR / "DocumentRegistry.json"


def compile_contract() -> None:
    sol_file = CONTRACTS_DIR / "DocumentRegistry.sol"
    result = subprocess.run(
        [
            "solc",
            "--combined-json",
            "abi,bin",
            "--evm-version",
            "paris",
            str(sol_file),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"solc error:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    combined = json.loads(result.stdout)
    key = f"{sol_file}:DocumentRegistry"
    contract = combined["contracts"][key]

    artifact = {
        "abi": json.loads(contract["abi"]),
        "bytecode": "0x" + contract["bin"],
    }
    OUTPUT_FILE.write_text(json.dumps(artifact, indent=2))
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    compile_contract()
