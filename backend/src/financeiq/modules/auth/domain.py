"""Email identity policy; complete address syntax validation belongs to Auth input."""


def normalize_email(value: str) -> str:
    """Keep provider-specific dots and suffixes while canonicalizing ASCII identity."""
    candidate = value.strip()
    if not candidate or any(not "!" <= character <= "~" for character in candidate):
        raise ValueError("Email must contain printable ASCII without whitespace.")
    return candidate.lower()
