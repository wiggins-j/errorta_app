"""code_edit spec — the pure anchored-splice: exact-match semantics and the
typed failure taxonomy (each code is a stable prefix of the raised message)."""
import pytest

from errorta_council.coding.edit_apply import (
    EDIT_EMPTY_OLD_STRING,
    EDIT_NO_CHANGE,
    EDIT_NO_MATCH,
    EDIT_NOT_UNIQUE,
    EditApplyError,
    apply_code_edit,
)


def test_unique_match_is_spliced() -> None:
    out = apply_code_edit("def f():\n    return 1\n", "return 1", "return 2")
    assert out == "def f():\n    return 2\n"


def test_only_the_single_occurrence_changes() -> None:
    # Surrounding content is untouched byte-for-byte.
    old = "a = 1\nb = 2\nc = 3\n"
    assert apply_code_edit(old, "b = 2", "b = 20") == "a = 1\nb = 20\nc = 3\n"


def test_replace_all_replaces_every_occurrence() -> None:
    out = apply_code_edit("x, x, x", "x", "y", replace_all=True)
    assert out == "y, y, y"


def test_replace_all_with_single_match_is_fine() -> None:
    assert apply_code_edit("only once", "once", "twice", replace_all=True) \
        == "only twice"


def test_empty_old_string_is_rejected() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("body", "", "new")
    assert exc.value.code == EDIT_EMPTY_OLD_STRING
    assert str(exc.value).startswith("edit_empty_old_string: ")


def test_no_change_is_rejected() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("body", "body", "body")
    assert exc.value.code == EDIT_NO_CHANGE
    assert str(exc.value).startswith("edit_no_change: ")


def test_no_match_is_rejected_and_names_exactness() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("def f():\n    return 1\n", "return  1", "return 2")
    assert exc.value.code == EDIT_NO_MATCH
    # The detail must remind the model that matching is exact — the whitespace
    # fumble above is the expected live failure mode.
    assert "exact" in exc.value.detail


def test_not_unique_is_rejected_with_count_and_recovery() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("x = 1\nx = 1\nx = 1\n", "x = 1", "x = 2")
    assert exc.value.code == EDIT_NOT_UNIQUE
    assert "3" in exc.value.detail
    assert "replace_all" in exc.value.detail


def test_matching_is_whitespace_sensitive() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("    indented\n", "indented \n", "x\n")
    assert exc.value.code == EDIT_NO_MATCH


def test_count_uses_non_overlapping_semantics() -> None:
    # "aaa" contains "aa" twice overlapping but ONCE non-overlapping — must
    # match str.replace's behaviour, which is what actually splices.
    assert apply_code_edit("aaa", "aa", "b") == "ba"


def test_unicode_content_round_trips() -> None:
    out = apply_code_edit("π = 3.14 # τ?\n", "3.14", "3.14159")
    assert out == "π = 3.14159 # τ?\n"


def test_whitespace_only_old_string_is_allowed_but_must_be_unique() -> None:
    # Only the EMPTY string is invalid per se; a whitespace anchor is legal and
    # subject to the same uniqueness rule as any other.
    assert apply_code_edit("a\t\tb", "\t\t", " ") == "a b"
