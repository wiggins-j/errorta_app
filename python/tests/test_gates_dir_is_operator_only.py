"""The gates directory is the operator's. No engine package may write there."""
from __future__ import annotations

import re
from pathlib import Path

_PKGS = ("errorta_council", "errorta_app", "errorta_slack", "errorta_liverun", "errorta_cli")
_WRITE = re.compile(r"gates_dir\(\)[^\n]*(write_text|open\(|mkdir|unlink|rename|replace)|"
                    r"[\"']gates[\"'][^\n]*(write_text|open\([^)]*[\"']w|mkdir|unlink|rename)")


def test_no_engine_code_writes_under_gates() -> None:
    root = Path(__file__).resolve().parents[1]
    hits = []
    for pkg in _PKGS:
        for py in (root / pkg).rglob("*.py"):
            for i, line in enumerate(py.read_text().splitlines(), 1):
                if _WRITE.search(line):
                    hits.append(f"{py.relative_to(root)}:{i}: {line.strip()}")
    assert not hits, "\n".join(hits)
