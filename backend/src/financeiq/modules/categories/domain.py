"""category_text_v1: CPython 3.14.10 / Unicode 16.0.0 text policy."""

from unicodedata import normalize


def _display_text(value: str) -> str:
    return " ".join(normalize("NFC", value).split())


def category_text_v1(value: str) -> str:
    """Normalize a comparison value without changing the source description."""
    return normalize("NFC", _display_text(value).casefold())


def _validated_display(value: str, limit: int) -> str:
    if not 1 <= len(value) <= limit:
        raise ValueError("Text length is outside the allowed range.")
    display = _display_text(value)
    if not 1 <= len(display) <= limit:
        raise ValueError("Normalized text length is outside the allowed range.")
    return display


def normalize_category_name(value: str) -> tuple[str, str]:
    """Return a validated display name and its matching, untruncated key."""
    name = _validated_display(value, 100)
    key = category_text_v1(name)
    if key == "без категории":
        raise ValueError("This category name is reserved.")
    return name, key


def normalize_rule_pattern(value: str) -> str:
    """Validate a display pattern; matching uses category_text_v1 separately."""
    return _validated_display(value, 200)
