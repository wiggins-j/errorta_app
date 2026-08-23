"""The gates directory is the operator's. No engine package may write there.

Classifier: a line (or an adjacent pair of lines) is a write-hit if a gates
ANCHOR — ``gates_dir(``, ``gate_path(``, or a quoted ``"gates"``/``'gates'`` —
and a write VERB co-occur, in either order, on the same line, or the anchor is
on line N and a verb is on line N+1 (catches the common
``p = gate_path(project_id)`` / ``p.write_text(...)`` two-line form without
tracking variable names). Verb-first forms (``open(gates_dir() / "x", "w")``)
and reversed forms are both single-line hits since the anchor/verb search is
order-independent.
"""
from __future__ import annotations

import re
from pathlib import Path

_PKGS = ("errorta_council", "errorta_app", "errorta_slack", "errorta_liverun", "errorta_cli",
         "errorta_tools", "errorta_export")
_ANCHOR = re.compile(r"gates_dir\(|gate_path\(|[\"']gates[\"']")
_VERB = re.compile(
    r"write_text|write_bytes|open\(|mkdir|unlink|rename|replace|touch|rmdir|copy|move"
)


def _line_hits(line: str) -> bool:
    return bool(_ANCHOR.search(line) and _VERB.search(line))


def _scan(lines: list[str]) -> list[int]:
    """1-based line numbers that trip the classifier (single-line or N/N+1)."""
    hits: list[int] = []
    for i, line in enumerate(lines):
        if _line_hits(line):
            hits.append(i + 1)
        elif _ANCHOR.search(line) and i + 1 < len(lines) and _VERB.search(lines[i + 1]):
            hits.append(i + 1)
    return hits


# --------------------------------------------------------------------------- #
# Self-test: the classifier itself must catch the shapes review flagged and
# leave plain reads alone, or this test is vacuous.
# --------------------------------------------------------------------------- #

_POSITIVES = (
    'gate_path(pid).write_text(yaml.safe_dump(doc))',            # verb after anchor
    'open(gates_dir() / "x.yaml", "w").write(doc)',               # verb-first, same line
    '"gates"; Path(base, "gates").mkdir(parents=True)',           # quoted anchor + verb
)
_POSITIVE_PAIRS = (
    ('p = gate_path(project_id)', 'p.write_text(yaml.safe_dump(doc))'),  # two-line form
)
_NEGATIVES = (
    "load_trusted_gate(project_id)",
    "gate_path(project_id).exists()",
    "gates_dir().is_symlink()",
    'doc = yaml.safe_load(path.read_text())',
)
_NEGATIVE_PAIRS = (
    ("gate_path(project_id).exists()", "return None"),
    ("some_other_dir()", 'f.write_text("x")'),
)


def test_classifier_catches_known_write_shapes() -> None:
    for line in _POSITIVES:
        assert _line_hits(line), f"classifier missed a known write shape: {line!r}"
    for first, second in _POSITIVE_PAIRS:
        assert _scan([first, second]) == [1], (
            f"classifier missed the two-line write form: {first!r} / {second!r}"
        )


def test_classifier_ignores_known_read_shapes() -> None:
    for line in _NEGATIVES:
        assert not _line_hits(line), f"classifier false-positived on a read: {line!r}"
    for first, second in _NEGATIVE_PAIRS:
        assert _scan([first, second]) == [], (
            f"classifier false-positived on a non-write pair: {first!r} / {second!r}"
        )


# --------------------------------------------------------------------------- #
# The real assertion: no engine package writes under gates/.
# --------------------------------------------------------------------------- #

def test_no_engine_code_writes_under_gates() -> None:
    root = Path(__file__).resolve().parents[1]
    hits = []
    for pkg in _PKGS:
        for py in (root / pkg).rglob("*.py"):
            lines = py.read_text().splitlines()
            for lineno in _scan(lines):
                hits.append(f"{py.relative_to(root)}:{lineno}: {lines[lineno - 1].strip()}")
    assert not hits, "\n".join(hits)
