"""Run-local Steward Packet store."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StewardPacketNotFound(LookupError):
    pass


class StewardPacketStore:
    def __init__(self, *, runs_dir: Path) -> None:
        self._runs_dir = Path(runs_dir)

    def write(self, run_id: str, packet: dict[str, Any]) -> Path:
        packet_id = _safe_packet_id(str(packet["packet_id"]))
        directory = self._dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{packet_id}.json"
        if path.exists():
            raise FileExistsError(f"steward packet already exists: {packet_id}")
        path.write_text(json.dumps(packet, indent=2, sort_keys=True))
        return path

    def read(self, run_id: str, packet_id: str) -> dict[str, Any]:
        path = self._dir(run_id) / f"{_safe_packet_id(packet_id)}.json"
        if not path.exists():
            raise StewardPacketNotFound(packet_id)
        return json.loads(path.read_text())

    def list(self, run_id: str) -> list[dict[str, Any]]:
        directory = self._dir(run_id)
        if not directory.exists():
            return []
        packets: list[dict[str, Any]] = []
        for path in sorted(directory.glob("sp_*.json")):
            try:
                packets.append(json.loads(path.read_text()))
            except Exception:
                continue
        return packets

    def latest(self, run_id: str) -> dict[str, Any] | None:
        packets = self.list(run_id)
        if not packets:
            return None
        return sorted(
            packets,
            key=lambda p: (
                str(p.get("created_at") or ""),
                str(p.get("packet_id") or ""),
            ),
        )[-1]

    def _dir(self, run_id: str) -> Path:
        return self._runs_dir / _safe_run_id(run_id) / "steward-packets"


def _safe_run_id(value: str) -> str:
    if not value or any(ch in value for ch in "/\\:"):
        raise ValueError("unsafe_run_id")
    return value


def _safe_packet_id(value: str) -> str:
    if not value.startswith("sp_"):
        raise ValueError("unsafe_packet_id")
    suffix = value[3:]
    if not suffix or any(ch not in "0123456789abcdef" for ch in suffix):
        raise ValueError("unsafe_packet_id")
    return value


__all__ = ["StewardPacketNotFound", "StewardPacketStore"]
