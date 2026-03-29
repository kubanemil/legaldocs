"""Document field extraction logic for the enclave.

Regex-based extraction of parties and dates from legal document text.
"""

import re
from typing import List


def _extract_parties(text: str) -> List[str]:
    """Extract party names from legal document text using multiple regex patterns."""
    parties: List[str] = []

    # Pattern 1: "between X and Y" clauses (case-insensitive)
    between_pattern = re.compile(
        r'\bbetween\s+(.+?)\s+and\s+(.+?)(?:\.|,|\n)',
        re.IGNORECASE,
    )
    for match in between_pattern.finditer(text):
        for group in match.groups():
            # Clean up: strip leading/trailing whitespace and common suffixes
            cleaned = group.strip().rstrip('.,;')
            if cleaned:
                parties.append(cleaned)

    # Pattern 2: "Party:" or "Parties:" labels
    party_label_pattern = re.compile(
        r'(?:Party|Parties)\s*:\s*(.+?)(?:\n|$)',
        re.IGNORECASE,
    )
    for match in party_label_pattern.finditer(text):
        parties.append(match.group(1).strip())

    # Pattern 3: Entity name patterns (Inc., LLC, Corp., Ltd., Corporation, Company)
    entity_pattern = re.compile(
        r'([A-Z][A-Za-z\s&.\'-]+?'
        r'(?:Inc\.?|LLC|Corp\.?|Ltd\.?|Corporation|Company|LP|LLP|Associates|Group|Holdings))'
        r'(?:\s*,|\s*\(|\s+a\s|\s*$|\s*\n)',
        re.MULTILINE,
    )
    for match in entity_pattern.finditer(text):
        cleaned = match.group(1).strip().rstrip('.,;')
        if cleaned:
            parties.append(cleaned)

    # Pattern 4: "hereinafter" references - extract the name before hereinafter
    hereinafter_pattern = re.compile(
        r'([A-Z][A-Za-z\s&.\'-]+?)\s*\(\s*(?:"|\'|)hereinafter',
        re.IGNORECASE,
    )
    for match in hereinafter_pattern.finditer(text):
        cleaned = match.group(1).strip().rstrip('.,;')
        if cleaned:
            parties.append(cleaned)

    # Pattern 5: "hereinafter "Name"" - extract the referenced name
    hereinafter_ref_pattern = re.compile(
        r'hereinafter\s+(?:referred\s+to\s+as\s+)?(?:"|\'|the\s+"|the\s+\')([A-Za-z\s]+?)(?:"|\'|\))',
        re.IGNORECASE,
    )
    for match in hereinafter_ref_pattern.finditer(text):
        cleaned = match.group(1).strip()
        if cleaned:
            parties.append(cleaned)

    return parties


def _extract_dates(text: str) -> List[str]:
    """Extract dates from legal document text using multiple regex patterns."""
    dates: List[str] = []

    # Pattern 1: ISO format YYYY-MM-DD
    iso_pattern = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')
    for match in iso_pattern.finditer(text):
        dates.append(match.group(1))

    # Pattern 2: US written format "Month DD, YYYY"
    us_written_pattern = re.compile(
        r'\b((?:January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+\d{1,2},\s*\d{4})\b'
    )
    for match in us_written_pattern.finditer(text):
        dates.append(match.group(1))

    # Pattern 3: Numeric US format MM/DD/YYYY
    numeric_pattern = re.compile(r'\b(\d{1,2}/\d{1,2}/\d{4})\b')
    for match in numeric_pattern.finditer(text):
        dates.append(match.group(1))

    # Pattern 4: Ordinal format "15th day of January, 2024"
    ordinal_pattern = re.compile(
        r'\b(\d{1,2}(?:st|nd|rd|th)\s+day\s+of\s+'
        r'(?:January|February|March|April|May|June|July|August|September|'
        r'October|November|December),?\s*\d{4})\b',
        re.IGNORECASE,
    )
    for match in ordinal_pattern.finditer(text):
        dates.append(match.group(1))

    return dates


def extract_fields(text: str) -> dict:
    """Extract parties and dates from legal document text.

    Returns:
        dict with keys "parties" (list of str) and "dates" (list of str),
        deduplicated and whitespace-stripped.
    """
    raw_parties = _extract_parties(text)
    raw_dates = _extract_dates(text)

    # Deduplicate while preserving order, strip whitespace
    seen_parties: set = set()
    parties: List[str] = []
    for p in raw_parties:
        stripped = p.strip()
        if stripped and stripped not in seen_parties:
            seen_parties.add(stripped)
            parties.append(stripped)

    seen_dates: set = set()
    dates: List[str] = []
    for d in raw_dates:
        stripped = d.strip()
        if stripped and stripped not in seen_dates:
            seen_dates.add(stripped)
            dates.append(stripped)

    return {"parties": parties, "dates": dates}
