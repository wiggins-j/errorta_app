"""F159 — shared file-path helpers for hot-file serialization.

Lives in its own module (not ``runner.py``) so ``topology.py`` can use it without
the topology→runner import cycle (``runner`` imports ``.topology`` at import
time). Pure functions, no engine state.

The canonical comparison form is the **git repo-relative full path** (what the
PR ``conflicts`` list and ``workspace.changed_paths`` record). The one wrinkle is
that a task's touched files are often only known from *prose* (a plan title /
detail), where the regex yields a bare basename (``mockData.ts``) that won't
string-equal the git full path (``src/mockData.ts``). :func:`paths_intersect`
bridges that with a basename fallback.
"""
from __future__ import annotations

import re
from typing import Any

# Filename-looking tokens in free text (the former ``runner._TARGET_PATH_RE``).
TARGET_PATH_RE = re.compile(
    r"(?<![\w./-])([\w./-]+\.(?:py|pyi|ts|tsx|js|jsx|rs|go|java|md|json|toml|"
    r"yaml|yml|css|html|sql|sh))(?![\w./-])"
)


def normalize_path(p: str) -> str:
    """Canonical comparison form: strip surrounding quotes/space and a leading
    ``./``. Case is preserved (paths are case-sensitive)."""
    s = str(p or "").strip().strip("`'\"(),;:")
    while s.startswith("./"):
        s = s[2:]
    return s


def _basename(p: str) -> str:
    return normalize_path(p).rsplit("/", 1)[-1]


def declared_target_paths(*parts: str) -> set[str]:
    """Filename-looking tokens extracted from free text (title/detail prose)."""
    out: set[str] = set()
    for part in parts:
        for match in TARGET_PATH_RE.findall(str(part or "")):
            cleaned = match.strip("`'\"(),;:")
            if cleaned and not cleaned.startswith(("/", "../")) \
                    and ".." not in cleaned.split("/"):
                out.add(normalize_path(cleaned))
    return out


def task_touched_paths(task: Any) -> set[str]:
    """The files a task will touch: declared ``target_files`` (in ``Task._extras``)
    when present, unioned with paths inferred from the title + detail prose. All
    normalized. Declared paths are the reliable signal; prose is the fallback."""
    declared: list[str] = []
    extras = getattr(task, "_extras", None)
    if isinstance(extras, dict):
        raw = extras.get("target_files")
        if isinstance(raw, (list, tuple)):
            declared = [str(x) for x in raw if x]
    paths = {normalize_path(p) for p in declared if p}
    paths |= declared_target_paths(
        getattr(task, "title", ""), getattr(task, "detail", ""))
    return {p for p in paths if p}


def paths_intersect(a: set[str], b: set[str]) -> bool:
    """True if any path in ``a`` collides with any in ``b`` — exact on the
    normalized full path, or by basename when one side is a bare filename (prose
    yields ``mockData.ts``; git records ``src/mockData.ts``)."""
    na = {normalize_path(x) for x in a}
    nb = {normalize_path(x) for x in b}
    if na & nb:
        return True
    a_bare = {x for x in na if "/" not in x}
    b_bare = {x for x in nb if "/" not in x}
    if a_bare & {_basename(x) for x in nb}:
        return True
    if b_bare & {_basename(x) for x in na}:
        return True
    return False
