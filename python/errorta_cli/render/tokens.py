"""Token usage view (``GET /usage-summary`` → ``usage``).

The F143 rollup: ``{by_member, by_route, by_role, total}``, each a bucket with
``input``/``output`` (effective headline = measured-where-present, estimated
otherwise), ``measured_*``/``estimated_*`` splits, ``turns`` + provenance turn
counts, and a ``coverage{measured_pct, estimated_pct}`` share.

We surface the split **honestly** with a measured-vs-estimated coverage meter so a
partly-estimated total reads as such (spec §9: "so the user trusts the numbers").
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render

_METER_WIDTH = 20


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _meter(measured_pct: float) -> Text:
    pct = max(0.0, min(100.0, float(measured_pct or 0)))
    filled = int(round(pct / 100 * _METER_WIDTH))
    bar = Text()
    bar.append("█" * filled, style="cli.ok")
    bar.append("░" * (_METER_WIDTH - filled), style="cli.warn")
    bar.append(f" {pct:.0f}% measured", style="cli.muted")
    return bar


def _bucket_row(table: Table, label: str, bucket: dict) -> None:
    coverage = bucket.get("coverage") or {}
    measured_pct = coverage.get("measured_pct")
    if measured_pct is None:
        total_turns = _int(bucket.get("turns"))
        measured_pct = (
            (_int(bucket.get("measured_turns")) / total_turns * 100)
            if total_turns
            else 0
        )
    table.add_row(
        label,
        f"{_int(bucket.get('input')):,}",
        f"{_int(bucket.get('output')):,}",
        f"{_int(bucket.get('turns')):,}",
        _meter(measured_pct),
    )


def _section(title: str, group: dict[str, Any], label_key: str) -> Table:
    table = Table(show_edge=False, pad_edge=False, box=None, title=title, title_justify="left")
    table.add_column(label_key, style="cli.key", no_wrap=True)
    table.add_column("input", justify="right", no_wrap=True)
    table.add_column("output", justify="right", no_wrap=True)
    table.add_column("turns", justify="right", no_wrap=True)
    table.add_column("coverage", no_wrap=True)
    if isinstance(group, dict):
        for key, bucket in group.items():
            if isinstance(bucket, dict):
                _bucket_row(table, str(key), bucket)
    return table


def render_tokens(payload: Any, verbosity: Any) -> str:
    usage = (payload or {}).get("usage") or {}
    total = usage.get("total") or {}
    if not usage:
        return render(muted("(no usage recorded)"))
    parts = [heading("Token usage")]
    if total:
        parts.append(_section("total", {"all": total}, "scope"))
    for key, title, label in (
        ("by_role", "by role", "role"),
        ("by_route", "by route", "route"),
        ("by_member", "by member", "member"),
    ):
        section = usage.get(key)
        if section:
            parts.append(_section(title, section, label))
    return render(*parts)
