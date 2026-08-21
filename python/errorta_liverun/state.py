"""Persisted live-run state: atomic state.json, append-only events.jsonl,
and the launch ledger that enforces caps across restarts (spec §3.6)."""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import MISSING, asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from errorta_app.paths import errorta_home

from .profile import Caps

PHASES = ("idle", "launching", "watching", "stopping", "stopped", "failed",
          "paused_awaiting_human", "lost_on_restart")
TERMINAL_PHASES = {"stopped", "failed", "paused_awaiting_human", "lost_on_restart"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunState:
    run_id: str
    profile_name: str
    project_id: str | None
    phase: str
    reason: str | None
    session_id: str
    step_index: int
    started_at: str
    launched_at: str | None
    ended_at: str | None
    owned_pgids: list[int] = field(default_factory=list)
    owned_remote_pidfiles: list[dict[str, str]] = field(default_factory=list)  # {"host","pidfile"}
    owned_tunnels: list[str] = field(default_factory=list)
    probe_last_ok: dict[str, str] = field(default_factory=dict)      # probe id -> iso
    probe_last_value: dict[str, str] = field(default_factory=dict)   # probe id -> last observed
    literals: dict[str, bool] = field(default_factory=dict)          # e.g. logoff_verified
    evidence_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunState":
        kwargs: dict[str, Any] = {}
        for name, f in cls.__dataclass_fields__.items():
            if name in d:
                kwargs[name] = d[name]
            elif f.default is not MISSING:
                kwargs[name] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                kwargs[name] = f.default_factory()  # type: ignore[misc]
            else:
                kwargs[name] = d.get(name)
        return cls(**kwargs)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root else errorta_home() / "liverun" / "runs"

    def new_run_id(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)

    def _dir(self, run_id: str) -> Path:
        return self._root / run_id

    def evidence_dir(self, run_id: str) -> Path:
        return self._dir(run_id) / "evidence"

    def save(self, state: RunState) -> None:
        _atomic_write(self._dir(state.run_id) / "state.json",
                      json.dumps(state.to_dict(), indent=1, sort_keys=True))

    def load(self, run_id: str) -> RunState | None:
        p = self._dir(run_id) / "state.json"
        if not p.is_file():
            return None
        try:
            return RunState.from_dict(json.loads(p.read_text()))
        except (ValueError, TypeError):
            return None

    def list_non_terminal(self) -> list[RunState]:
        if not self._root.is_dir():
            return []
        out = []
        for d in sorted(self._root.iterdir()):
            st = self.load(d.name)
            if st is not None and st.phase not in TERMINAL_PHASES:
                out.append(st)
        return out

    def append_event(self, run_id: str, kind: str, detail: dict[str, Any]) -> int:
        p = self._dir(run_id) / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        seq = len(self.events(run_id)) + 1
        with p.open("a") as fh:
            fh.write(json.dumps({"seq": seq, "at": now_iso(), "kind": kind, "detail": detail}) + "\n")
        return seq

    def events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        p = self._dir(run_id) / "events.jsonl"
        if not p.is_file():
            return []
        out = []
        for line in p.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if int(ev.get("seq", 0)) > after_seq:
                out.append(ev)
        return out


class LaunchLedger:
    """Append-only record of launches + outcomes; caps are computed over it so a
    burst survives a sidecar restart (the brain's own budget reasoning)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else errorta_home() / "liverun" / "launches.jsonl"

    def _rows(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        rows = []
        for line in self._path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _append(self, row: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def record(self, profile_name: str, run_id: str, at: float) -> None:
        self._append({"kind": "launch", "profile": profile_name, "run_id": run_id, "at": at})

    def record_outcome(self, run_id: str, *, failed: bool) -> None:
        self._append({"kind": "outcome", "run_id": run_id, "failed": bool(failed)})

    def check(self, profile_name: str, caps: Caps, now: float) -> str | None:
        rows = self._rows()
        launches = [r for r in rows if r.get("kind") == "launch" and r.get("profile") == profile_name]
        if not launches:
            return None
        last = max(float(r["at"]) for r in launches)
        if now - last < caps.min_launch_gap_s:
            return "cap_gap"
        if sum(1 for r in launches if now - float(r["at"]) < 3600) >= caps.max_launches_per_hour:
            return "cap_hourly"
        if sum(1 for r in launches if now - float(r["at"]) < 86400) >= caps.max_launches_per_day:
            return "cap_daily"
        outcomes = {r["run_id"]: bool(r["failed"]) for r in rows if r.get("kind") == "outcome"}
        streak = 0
        for r in sorted(launches, key=lambda r: float(r["at"]), reverse=True):
            failed = outcomes.get(r["run_id"])
            if failed is None:
                continue
            if not failed:
                break
            streak += 1
        if streak >= caps.max_consecutive_failed_cycles:
            return "cap_consecutive_failures"
        return None


__all__ = ["PHASES", "TERMINAL_PHASES", "RunState", "RunStore", "LaunchLedger", "now_iso"]
