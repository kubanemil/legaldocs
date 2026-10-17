"""Regex extraction of parties and dates from legal text."""

import re
from typing import Final

_MONTHS: Final = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
_ENTITY_SUFFIX: Final = (
    r"(?:Inc\.?|LLC|Corp\.?|Ltd\.?|Corporation|Company|LP|LLP|Associates|Group|Holdings)"
)

_PARTY_PATTERNS: Final = (
    re.compile(r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\.|,|\n)", re.IGNORECASE),
    re.compile(r"(?:Party|Parties)\s*:\s*(.+?)(?:\n|$)", re.IGNORECASE),
    re.compile(
        rf"([A-Z][A-Za-z\s&.'-]+?{_ENTITY_SUFFIX})(?:\s*,|\s*\(|\s+a\s|\s*$|\s*\n)", re.MULTILINE
    ),
    re.compile(r"([A-Z][A-Za-z\s&.'-]+?)\s*\(\s*(?:\"|'|)hereinafter", re.IGNORECASE),
    re.compile(
        r"hereinafter\s+(?:referred\s+to\s+as\s+)?(?:\"|'|the\s+\"|the\s+')([A-Za-z\s]+?)(?:\"|'|\))",
        re.IGNORECASE,
    ),
)

_DATE_PATTERNS: Final = (
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(rf"\b((?:{_MONTHS})\s+\d{{1,2}},\s*\d{{4}})\b"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"),
    re.compile(
        rf"\b(\d{{1,2}}(?:st|nd|rd|th)\s+day\s+of\s+(?:{_MONTHS}),?\s*\d{{4}})\b", re.IGNORECASE
    ),
)


def extract_fields(text: str) -> dict[str, list[str]]:
    return {
        "parties": _unique(_matches(_PARTY_PATTERNS, text)),
        "dates": _unique(_matches(_DATE_PATTERNS, text)),
    }


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> list[str]:
    return [
        group.strip().rstrip(".,;")
        for pattern in patterns
        for match in pattern.finditer(text)
        for group in match.groups()
        if group
    ]


def _unique(values: list[str]) -> list[str]:
    return [value for value in dict.fromkeys(value.strip() for value in values) if value]
