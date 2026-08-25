"""code_edit — the pure anchored find/replace a dev turn splices a file with.

Why this tool exists (live incident, senditai-ng task t-e75a9c4d5a2e): the only
dev write tool was ``code_write``, which re-emits the WHOLE file — so a correct
fix to a 1600-line module collapsed under the turn's output budget and the F140
guard (rightly) blocked the truncated rewrite. Any fix to a large file was
structurally impossible. ``code_edit`` makes the write proportional to the
CHANGE: the model quotes the exact text to replace and its replacement.

Semantics are Claude Code's Edit tool, which strong models already know:
``old_string`` is matched EXACTLY (no regex, no whitespace normalization, no
line numbers — models fumble line arithmetic but quote text reliably) and must
occur exactly once unless ``replace_all``. Occurrence counting is
non-overlapping (``str.count``), matching the ``str.replace`` that splices.

Failures raise :class:`EditApplyError` whose message starts with a stable code
(``edit_no_match: ...``) — the tool-event / carry-forward channel matches on
that prefix. Pure/dependency-free so it is unit-testable without git or a
workspace (the ``write_guard`` idiom).
"""
from __future__ import annotations

EDIT_EMPTY_OLD_STRING = "edit_empty_old_string"
EDIT_NO_CHANGE = "edit_no_change"
EDIT_NO_MATCH = "edit_no_match"
EDIT_NOT_UNIQUE = "edit_not_unique"


class EditApplyError(ValueError):
    """A ``code_edit`` that cannot be applied; ``str()`` is ``"{code}: {detail}"``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def apply_code_edit(old_content: str, old_string: str, new_string: str, *,
                    replace_all: bool = False) -> str:
    """Return ``old_content`` with ``old_string`` replaced by ``new_string``.

    Exactly-once matching unless ``replace_all`` (which accepts any count >= 1).
    Raises :class:`EditApplyError` — never returns partial/unchanged content.
    """
    if old_string == "":
        raise EditApplyError(
            EDIT_EMPTY_OLD_STRING,
            "old_string must be non-empty — quote the exact text to replace "
            "(code_edit cannot create a file; use code_write for that)")
    if old_string == new_string:
        raise EditApplyError(
            EDIT_NO_CHANGE, "old_string and new_string are identical")
    count = old_content.count(old_string)
    if count == 0:
        raise EditApplyError(
            EDIT_NO_MATCH,
            "old_string was not found in the file — matching is exact "
            "(whitespace and indentation included); copy the text verbatim "
            "from the current file contents")
    if count > 1 and not replace_all:
        raise EditApplyError(
            EDIT_NOT_UNIQUE,
            f"old_string matches {count} times — enlarge the anchor with "
            "surrounding lines so it is unique, or set \"replace_all\": true "
            "to change every occurrence")
    return old_content.replace(old_string, new_string)


__all__ = [
    "EDIT_EMPTY_OLD_STRING", "EDIT_NO_CHANGE", "EDIT_NO_MATCH",
    "EDIT_NOT_UNIQUE", "EditApplyError", "apply_code_edit",
]
