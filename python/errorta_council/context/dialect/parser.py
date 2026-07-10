"""Tolerant parser for F036 digest_v1 model output."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_CONFIDENCE = {"high", "medium", "low"}


@dataclass(frozen=True)
class DigestParseResult:
    ok: bool
    digest: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


def parse_digest_v1(content: str, *, known_citations: set[str] | None = None) -> DigestParseResult:
    obj_text = _first_json_object(content or "")
    if obj_text is None:
        return DigestParseResult(False, warnings=["no_json_object"])
    try:
        raw = json.loads(obj_text)
    except json.JSONDecodeError:
        return DigestParseResult(False, warnings=["invalid_json"])
    if not isinstance(raw, dict) or raw.get("v") != "digest_v1":
        return DigestParseResult(False, warnings=["bad_digest_version"])
    warnings: list[str] = []
    digest: dict[str, Any] = {
        "v": "digest_v1",
        "position": str(raw.get("position") or ""),
        "claims": [],
        "agree": _list_of_dicts(raw.get("agree")),
        "dispute": _list_of_dicts(raw.get("dispute")),
        "delta": raw.get("delta") if raw.get("delta") is None else str(raw.get("delta")),
        "open": [str(x) for x in (raw.get("open") or [])],
        "answer_fragment": str(raw.get("answer_fragment") or ""),
    }
    known = known_citations or set()
    for idx, claim in enumerate(_list_of_dicts(raw.get("claims"))):
        confidence = str(claim.get("confidence") or "medium")
        if confidence not in _CONFIDENCE:
            warnings.append(f"claim_{idx}_bad_confidence")
            confidence = "medium"
        cites = []
        for cite in claim.get("cites") or []:
            cite_s = str(cite)
            if known and cite_s not in known:
                warnings.append(f"unknown_cite:{cite_s}")
                continue
            cites.append(cite_s)
        digest["claims"].append({
            "id": str(claim.get("id") or f"k{idx + 1}"),
            "text": str(claim.get("text") or ""),
            "cites": cites,
            "confidence": confidence,
        })
    return DigestParseResult(True, digest=digest, warnings=warnings)


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [dict(x) for x in (value or []) if isinstance(x, dict)]


def _first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


__all__ = ["DigestParseResult", "parse_digest_v1"]
