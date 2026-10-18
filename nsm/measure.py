"""Reproducible PCRs for the enclave image, computed from its build inputs.

PCR0 covers every input to the enclave image, PCR1 the runtime layer (Dockerfile with a
digest-pinned base and hash-locked dependencies) and PCR2 the application code. This
mirrors Nitro's split into image, kernel/bootstrap and application.
"""

import argparse
import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Final

ENCLAVE_DIRS: Final = ("enclave", "protocol")
RUNTIME_FILES: Final = frozenset({"enclave/Dockerfile", "enclave/requirements.txt"})
PCR_BYTES: Final = 48


def build_inputs(root: Path) -> list[str]:
    inputs = []
    for directory in ENCLAVE_DIRS:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if path.is_file() and not _ignored(relative):
                inputs.append(relative.as_posix())
    if not set(inputs) >= RUNTIME_FILES:
        raise FileNotFoundError(f"{root} is missing enclave build inputs")
    return sorted(inputs)


def measure(root: Path) -> dict[int, bytes]:
    inputs = build_inputs(root)
    return {
        0: _measure(root, inputs),
        1: _measure(root, (p for p in inputs if p in RUNTIME_FILES)),
        2: _measure(root, (p for p in inputs if p not in RUNTIME_FILES)),
    }


def extend(pcr: bytes, measurement: bytes) -> bytes:
    return hashlib.sha384(pcr + measurement).digest()


def _measure(root: Path, paths: Iterable[str]) -> bytes:
    pcr = bytes(PCR_BYTES)
    for path in paths:
        pcr = extend(
            pcr, hashlib.sha384(path.encode() + b"\0" + (root / path).read_bytes()).digest()
        )
    return pcr


def _ignored(path: Path) -> bool:
    return any(part.startswith(".") or part == "__pycache__" for part in path.parts) or (
        path.suffix == ".pyc"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the enclave PCRs for a source tree.")
    parser.add_argument("root", nargs="?", type=Path, default=Path())
    parser.add_argument("--all", action="store_true", help="print PCR0-2 instead of PCR0 only")
    args = parser.parse_args()
    pcrs = measure(args.root)
    if args.all:
        for index, value in pcrs.items():
            print(f"PCR{index} {value.hex()}")
    else:
        print(pcrs[0].hex())


if __name__ == "__main__":
    main()
