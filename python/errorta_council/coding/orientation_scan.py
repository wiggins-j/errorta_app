"""F135 — North Star inference ("orientation scan").

A bounded, read-only analyst call that reads an imported repo and PROPOSES a
North Star + Definition of Done (never writes them — the human accepts in the
F122 editor). It is NOT a full run: it resolves a single model route, makes ONE
gateway call, and returns a structured proposal.

Trust boundaries (F135 D4 / Review #1, #5, #10):
  * reads the user repo directly via the skip-set-honoring bounded reader (no
    ApplyWorkspace copy, no secrets in the prompt);
  * the single gateway call is the only egress; the route layer guards it with
    ``refuse_local_dataplane_if_remote`` and Tauri-origin;
  * a low-signal repo (empty / no README) yields an honest empty proposal, never
    a fabricated North Star.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .ledger import LedgerStore, _now
from .topology import PM, coding_role_of

MemberCaller = Callable[[dict[str, Any], str], str]


class ScanError(RuntimeError):
    """Orientation scan could not run (stable reason code)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def resolve_scan_member(store: LedgerStore, *, route_id: str | None = None,
                        ) -> dict[str, Any]:
    """Resolve the member (route) the scan will call.

    Preference: the project's saved run_config team (PM member first, else the
    first enabled member). If no team is saved, an explicit ``route_id`` is
    required — there is no standing "PM model" before a run has ever started.
    Raises :class:`ScanError('no_route')` when neither is available.
    """
    members = [m for m in (store.get_run_config().get("members") or [])
               if isinstance(m, dict) and m.get("enabled", True)]
    if members:
        pm = next((m for m in members if coding_role_of(m) == PM), None)
        chosen = pm or members[0]
        return dict(chosen)
    if route_id:
        return {
            "id": "scan",
            "role": "answerer",
            "coding_role": PM,
            "gateway_route_id": str(route_id),
            "provider_kind": str(route_id).split(".", 1)[0] if "." in str(route_id) else "local",
            "model": "",
            "enabled": True,
        }
    raise ScanError("no_route")


def _build_prompt(read: dict[str, Any], grounding_text: str = "") -> str:
    ground = f"\n\nAdditional project memory:\n{grounding_text}\n" if grounding_text else ""
    return (
        "You are onboarding an existing software project. Read the excerpts below "
        "(README and source/manifest files) and infer what the project is.\n\n"
        f"{read.get('blob', '')}{ground}\n"
        "Reply with ONLY a JSON object, no prose, of this exact shape:\n"
        '{"north_star": "one or two sentences: the project\'s enduring vision", '
        '"definition_of_done": "what finished looks like for the whole project", '
        '"summary": "a plain-English paragraph describing the project", '
        '"detected_stack": ["languages/frameworks you detected"], '
        '"suggested_first_tasks": ["concrete next tasks a team could pick up"]}\n'
        "If the excerpts are too thin to tell what the project is, return empty "
        'strings for north_star/definition_of_done and say so in "summary". Never '
        "invent a purpose that is not supported by what you read."
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _low_signal(model: str, files: list[str], reason: str) -> dict[str, Any]:
    return {
        "north_star": "",
        "definition_of_done": "",
        "summary": reason,
        "detected_stack": [],
        "suggested_first_tasks": [],
        "source_refs": files,
        "generated_at": _now(),
        "model": model,
        "low_signal": True,
        "accepted": False,
        "accepted_at": None,
    }


def build_proposal(read: dict[str, Any], raw_reply: str, *, model: str,
                   ) -> dict[str, Any]:
    """Turn a model reply + the bounded read into a stored-proposal dict."""
    files = list(read.get("files") or [])
    obj = _extract_json(raw_reply)
    if not obj:
        return _low_signal(
            model, files,
            "The model did not return a usable proposal; add a North Star manually.")

    def _s(key: str) -> str:
        v = obj.get(key)
        return str(v).strip() if isinstance(v, (str, int, float)) else ""

    def _list(key: str) -> list[str]:
        v = obj.get(key)
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    north = _s("north_star")
    return {
        "north_star": north,
        "definition_of_done": _s("definition_of_done"),
        "summary": _s("summary"),
        "detected_stack": _list("detected_stack"),
        "suggested_first_tasks": _list("suggested_first_tasks"),
        "source_refs": files,
        "generated_at": _now(),
        "model": model,
        "low_signal": not north,
        "accepted": False,
        "accepted_at": None,
    }


def run_orientation_scan(store: LedgerStore, *, member: dict[str, Any],
                         caller: MemberCaller, repo_path: str | None,
                         grounding_text: str = "") -> dict[str, Any]:
    """Read ``repo_path``, run one gateway call via ``caller``, and return (and
    persist) the proposal. A low-signal repo skips the model call entirely."""
    from errorta_tools.runner.repo_reader import read_bounded

    model = str(member.get("gateway_route_id") or member.get("model") or "")
    if not repo_path:
        proposal = _low_signal(model, [], "No repository path is set for this project.")
        return store.save_orientation_proposal(proposal)

    read = read_bounded(repo_path)
    if read.get("empty"):
        proposal = _low_signal(
            model, read.get("files") or [],
            "The repository has no readable text files to infer a North Star from.")
        return store.save_orientation_proposal(proposal)

    prompt = _build_prompt(read, grounding_text)
    raw = caller(member, prompt)
    proposal = build_proposal(read, raw, model=model)
    return store.save_orientation_proposal(proposal)
