"""Filesystem helpers for model gateway state."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from errorta_app.paths import (
    model_gateway_audit_path,
    model_gateway_budget_path,
    model_gateway_settings_path,
)


def gateway_dir() -> Path:
    p = model_gateway_settings_path().parent
    p.mkdir(parents=True, exist_ok=True)
    return p


def policy_path() -> Path:
    return model_gateway_settings_path()


def audit_path() -> Path:
    return model_gateway_audit_path()


def budget_ledger_path() -> Path:
    return model_gateway_budget_path()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows
