"""Versioned category text normalization, without HTTP or database state."""

import unicodedata

import pytest

from financeiq.modules.categories.domain import (
    category_text_v1,
    normalize_category_name,
    normalize_rule_pattern,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  ОПЛАТА\t\u00a0КАФЕ\nУ ДОМА  ", "оплата кафе у дома"),
        ("Cafe\u0301", "café"),
        ("Straße", "strasse"),
        ("\u01f0", "\u01f0"),  # casefold decomposes this; the final NFC restores it.
        ("10% _ * .", "10% _ * ."),
        ("", ""),
        ("\t \u00a0\n", ""),
    ],
)
def test_category_text_v1(value: str, expected: str) -> None:
    assert category_text_v1(value) == expected
    assert category_text_v1(expected) == expected


@pytest.mark.parametrize(
    ("first", "second"), [("е", "ё"), ("Cafe", "Café"), ("a", "а")]
)
def test_meaningful_text_differences_are_preserved(first: str, second: str) -> None:
    assert category_text_v1(first) != category_text_v1(second)


def test_display_values_preserve_case_and_return_matching_key() -> None:
    original = " \tCafe\u0301\u00a0Straße "
    assert normalize_category_name(original) == ("Café Straße", "café strasse")
    assert normalize_rule_pattern(original) == "Café Straße"
    assert original == " \tCafe\u0301\u00a0Straße "


@pytest.mark.parametrize("length", [1, 100])
def test_name_length_boundaries_and_expanding_keys(length: int) -> None:
    name, key = normalize_category_name("ß" * length)
    assert name == "ß" * length
    assert key == "ss" * length


@pytest.mark.parametrize("length", [1, 200])
def test_pattern_length_boundaries_and_expanding_comparison(length: int) -> None:
    pattern = normalize_rule_pattern("ß" * length)
    assert pattern == "ß" * length
    assert category_text_v1(pattern) == "ss" * length


@pytest.mark.parametrize(
    "value",
    ["", " \t\n\u00a0", "я" * 101, " " * 100 + "a", "\u0344" * 51],
)
def test_invalid_name_lengths_are_rejected_without_truncation(value: str) -> None:
    # U+0344 expands under NFC, so both input and normalized limits matter.
    with pytest.raises(ValueError):
        normalize_category_name(value)


@pytest.mark.parametrize(
    "value", ["Без категории", " БЕЗ\t\u00a0КАТЕГОРИИ ", "без\nкатегории"]
)
def test_reserved_name_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_category_name(value)


@pytest.mark.parametrize(
    "value",
    ["", " \t\n\u00a0", "я" * 201, " " * 200 + "a", "\u0344" * 101],
)
def test_invalid_patterns_are_rejected_without_truncation(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_rule_pattern(value)


def test_normalization_unicode_tables_are_pinned() -> None:
    assert unicodedata.unidata_version == "16.0.0"
