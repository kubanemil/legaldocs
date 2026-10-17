"""Write contracts/ReceiptRegistry.json (ABI and bytecode) from `forge build` output."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILT = ROOT / "contracts/out/ReceiptRegistry.sol/ReceiptRegistry.json"
ARTIFACT = ROOT / "contracts/ReceiptRegistry.json"


def render() -> str:
    built = json.loads(BUILT.read_text())
    return (
        json.dumps({"abi": built["abi"], "bytecode": built["bytecode"]["object"]}, indent=2) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    if parser.parse_args().check:
        if ARTIFACT.read_text() != render():
            sys.exit("contracts/ReceiptRegistry.json is stale: run ./do contracts")
    else:
        ARTIFACT.write_text(render())


if __name__ == "__main__":
    main()
