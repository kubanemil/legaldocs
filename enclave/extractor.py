"""Every repetition is bounded, so a hostile document cannot make extraction quadratic."""

import re
from typing import Any, Final

MAX_SCAN_CHARS: Final = 100_000

_MONTHS: Final = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
_ENTITY_SUFFIX: Final = (
    r"(?:Inc\.?|LLC|Corp\.?|Ltd\.?|Corporation|Company|LP|LLP|Associates|Group|Holdings)"
)
_NAME: Final = r"[A-Z][A-Za-z\s&.'-]{1,80}?"
_GAP: Final = r"\s{1,8}"

_PARTY_PATTERNS: Final = (
    re.compile(
        rf"\bbetween{_GAP}(.{{1,80}}?){_GAP}and{_GAP}([^.,\n]{{1,80}}+)[.,\n]", re.IGNORECASE
    ),
    re.compile(r"\b(?:Party|Parties)[ \t]{0,8}:[ \t]{0,8}([^\n]{1,120}+)", re.IGNORECASE),
    re.compile(rf"({_NAME}{_ENTITY_SUFFIX})(?=\s{{0,8}}[,(\n]|{_GAP}a\s|\s{{0,8}}$)", re.MULTILINE),
    re.compile(rf"({_NAME})\s{{0,8}}\(\s{{0,8}}[\"']?hereinafter", re.IGNORECASE),
    re.compile(
        rf"hereinafter{_GAP}(?:referred{_GAP}to{_GAP}as{_GAP})?(?:the{_GAP})?[\"']"
        r"([A-Za-z\s]{1,80}?)[\"')]",
        re.IGNORECASE,
    ),
)

_DATE_PATTERNS: Final = (
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(rf"\b((?:{_MONTHS}){_GAP}\d{{1,2}},\s{{0,8}}\d{{4}})\b"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"),
    re.compile(
        rf"\b(\d{{1,2}}(?:st|nd|rd|th){_GAP}day{_GAP}of{_GAP}(?:{_MONTHS}),?\s{{0,8}}\d{{4}})\b",
        re.IGNORECASE,
    ),
)


def extract_fields(text: str) -> dict[str, Any]:
    scanned = text[:MAX_SCAN_CHARS]
    return {
        "parties": _unique(_matches(_PARTY_PATTERNS, scanned)),
        "dates": _unique(_matches(_DATE_PATTERNS, scanned)),
        "truncated": len(text) > MAX_SCAN_CHARS,
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
