import time

import pytest

from enclave.extractor import MAX_SCAN_CHARS, extract_fields

N = MAX_SCAN_CHARS


def test_parties_and_dates_are_extracted() -> None:
    fields = extract_fields(
        "This Agreement is entered into as of March 15, 2025, by and between Acme Corporation, "
        "a Delaware corporation, and Pinnacle Industries LLC, a Nevada company. "
        "It runs from 2025-04-01 to 03/31/2027."
    )
    assert "Pinnacle Industries LLC" in fields["parties"]
    assert "Acme Corporation, a Delaware corporation" in fields["parties"]
    assert fields["dates"] == ["2025-04-01", "March 15, 2025", "03/31/2027"]
    assert fields["truncated"] is False


@pytest.mark.parametrize(
    "hostile",
    [
        "A" + "a" * N,
        "A" * N,
        "Aa " * (N // 3),
        "between " * (N // 8),
        "between " + "and " * (N // 4),
        "between x and " * (N // 14),
        "Party: " * (N // 7),
        'hereinafter "' * (N // 13),
        "January " * (N // 8),
        "A" + " " * N,
    ],
    ids=[
        "one long word",
        "capitals",
        "short words",
        "between",
        "between and and",
        "between x and",
        "party labels",
        "hereinafter",
        "months",
        "whitespace",
    ],
)
def test_hostile_input_is_scanned_in_linear_time(hostile: str) -> None:
    started = time.monotonic()
    extract_fields(hostile)
    assert time.monotonic() - started < 3


def test_text_beyond_the_scan_limit_is_reported_not_scanned() -> None:
    fields = extract_fields("x" * N + " between Late Corp and Later LLC.")
    assert fields["truncated"] is True
    assert fields["parties"] == []
