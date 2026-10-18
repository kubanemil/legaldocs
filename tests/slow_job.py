"""A job the worker-pool tests can make overrun. Kept tiny so spawned workers start fast."""

import time
from typing import Any

from enclave.extractor import extract_fields


def stall_on_slow(text: str) -> dict[str, Any]:
    if text.startswith("slow"):
        time.sleep(60)
    return extract_fields(text)
