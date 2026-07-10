"""F145 Slice 2 — the AI Wizard: a conversation that produces a runnable charter.

The Wizard is a pre-project conversation with a user-picked model. Its system
prompt is the PM reference context (``pm_reference.build_pm_reference_context``)
plus the runnable-by-construction intake contract. Each turn the model returns a
strict-ish JSON object with its reply plus the structured intake it has captured
so far; the Wizard cannot ``finalize`` into a charter until the intake is complete
(North Star, audience, modality, a runnable Definition of Done, and an entrypoint).

Sessions are **ephemeral** — stored under ``${ERRORTA_HOME}/council/wizard-sessions``
and deleted once the project is created (or on discard). Nothing here creates a
project; ``routes/coding.py`` drives create-on-accept.

The model call is injected (``caller``) so it is fully unit-testable without egress.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ledger import _atomic_write_json, _now

MODALITIES = ("static", "server", "cli", "desktop", "binary", "container")
# The runnable-by-construction completion contract (PM_REFERENCE §11): these must
# be captured before the Wizard may finalize.
REQUIRED_CHARTER_FIELDS = (
    "north_star", "audience", "modality", "definition_of_done", "entrypoint")


def _as_bool(value: Any) -> bool:
    """Coerce a charter flag to bool. A model may emit the string "false"/"no" —
    ``bool("false")`` is True, so parse strings explicitly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)

MemberCaller = Callable[[dict[str, Any], str], str]


class WizardError(Exception):
    """Wizard session/validation failure."""


# --------------------------------------------------------------------------- #
# Session store (ephemeral)
# --------------------------------------------------------------------------- #
@dataclass
class WizardSession:
    session_id: str
    model_route: str
    messages: list[dict[str, str]] = field(default_factory=list)  # {role, text}
    charter: dict[str, Any] = field(default_factory=dict)
    ready: bool = False
    missing: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model_route": self.model_route,
            "messages": [dict(m) for m in self.messages],
            "charter": dict(self.charter),
            "ready": self.ready,
            "missing": list(self.missing),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WizardSession":
        return cls(
            session_id=str(raw.get("session_id") or ""),
            model_route=str(raw.get("model_route") or ""),
            messages=[dict(m) for m in raw.get("messages", []) if isinstance(m, dict)],
            charter=dict(raw.get("charter") or {}),
            ready=bool(raw.get("ready")),
            missing=[str(x) for x in raw.get("missing", [])],
            created_at=str(raw.get("created_at") or ""),
        )


def _sessions_dir() -> Path:
    from errorta_app.paths import errorta_home

    d = errorta_home() / "council" / "wizard-sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(session_id: str) -> Path:
    from errorta_export.safe_path import safe_segment

    return _sessions_dir() / f"{safe_segment(session_id)}.json"


def new_session(model_route: str) -> WizardSession:
    session = WizardSession(
        session_id=f"wiz-{uuid.uuid4().hex[:12]}",
        model_route=model_route,
        created_at=_now(),
    )
    _save(session)
    return session


def _save(session: WizardSession) -> None:
    _atomic_write_json(_session_path(session.session_id), session.to_dict())


def get_session(session_id: str) -> WizardSession | None:
    try:
        path = _session_path(session_id)
    except Exception:
        return None
    if not path.is_file():
        return None
    try:
        return WizardSession.from_dict(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
        return None


def discard_session(session_id: str) -> None:
    try:
        _session_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Prompt + turn
# --------------------------------------------------------------------------- #
OPENING = (
    "Hey — let's shape your project. Tell me what you want to build, who it's "
    "for, and what “done” looks like. I'll ask a few questions, then set "
    "up a team that can build and run it."
)

_TURN_INSTRUCTIONS = """
You are the PM running the "AI Wizard" — a single conversation that must end with
a *runnable* project fully set up. Use the operator's manual and LIVE STATE above.

Rules:
- Ask for what you need; assume sensible defaults (from the manual) only for
  minor details. Never leave the project half-configured.
- You MUST explicitly ASK the user about the TEAM before finishing — do not
  silently default it:
  * How the team should run: fully AUTONOMOUS (it works without pausing to ask
    you) or not (it checks in). Record this in `autonomous`.
  * Which model tier / team composition to use, which sets the model family each
    role (PM, developers, reviewer) runs on. Offer the four recipes and record
    the choice in `team_recipe`: `fast_cheap` (cheaper/faster models),
    `balanced`, `highest_quality` (strongest models), or `private_offline`
    (fully local). Ground the tiers in LIVE STATE's `available_routes` — if the
    user names a model that isn't there, say so and offer what is.
- Before you may finish, you MUST have: north_star, audience, modality (one of
  static/server/cli/desktop/binary/container), a definition_of_done that includes
  a runnable check, an entrypoint (the concrete file the team must produce, e.g.
  index.html or main.py), the user's `autonomous` choice (true/false), and a
  `team_recipe`.

Reply with a SINGLE JSON object and nothing else:
{
  "reply": "<what to say to the user next — one short paragraph>",
  "charter": {
    "north_star": "", "audience": "", "modality": "",
    "definition_of_done": "", "entrypoint": "", "scope_notes": "",
    "team_recipe": "fast_cheap|balanced|highest_quality|private_offline (ask, pick one)",
    "autonomous": null
  },
  "ready": false,
  "missing": ["<required fields still missing>"]
}
Set "ready": true only when EVERY required field is filled — including an explicit
`autonomous` (true/false) and a chosen `team_recipe`. Keep prior charter values;
only add/refine.
"""


def build_prompt(session: WizardSession, *, context: str) -> str:
    """The full model prompt: reference context + instructions + the transcript."""
    lines = [context, _TURN_INSTRUCTIONS, "\n## Conversation so far\n"]
    for m in session.messages:
        who = "User" if m.get("role") == "user" else "PM"
        lines.append(f"{who}: {m.get('text', '')}")
    lines.append("PM (respond with the JSON object now):")
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Lenient parse: the last balanced JSON object in the text (mirrors F127's
    tolerant turn parsing — a model may wrap the object in prose or a fence)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    # also try the widest {...} span
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


def _synthetic_member(route: str) -> dict[str, Any]:
    provider = route.split(".", 1)[0] if "." in route else "local"
    return {
        "id": "wizard", "role": "answerer", "coding_role": "pm",
        "gateway_route_id": route, "provider_kind": provider,
        "model_mode": "single",
        "turn_limits": {"timeout_seconds": 120, "max_output_tokens": 2048},
    }


def _is_explicit_bool(value: Any) -> bool:
    """True when the user actually ANSWERED an autonomy yes/no — presence, not
    truthiness (False is a valid answer). A bool or a recognized yes/no string
    counts; ``None``/absent/gibberish does not, so the Wizard keeps asking."""
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip().lower() in (
            "true", "false", "yes", "no", "on", "off", "1", "0")
    return False


def _compute_missing(charter: dict[str, Any]) -> list[str]:
    from .recipes import RECIPES

    missing = [f for f in REQUIRED_CHARTER_FIELDS
               if not str(charter.get(f) or "").strip()]
    # F145: the Wizard must ASK the user about the team, not silently default it.
    # team_recipe must be an explicit valid recipe (rejects the enum-template
    # placeholder "balanced|fast_cheap|..."); autonomous must be an explicit yes/no.
    if str(charter.get("team_recipe") or "").strip() not in RECIPES:
        missing.append("team_recipe")
    if not _is_explicit_bool(charter.get("autonomous")):
        missing.append("autonomous")
    return missing


def _default_caller() -> MemberCaller:
    from errorta_council.coding.runner import gateway_member_caller
    from errorta_council.gateway_local import LocalGateway

    return gateway_member_caller(LocalGateway())


def run_turn(
    session: WizardSession, user_message: str, *, context: str,
    caller: MemberCaller | None = None,
) -> WizardSession:
    """Append the user turn, call the model, parse its structured reply, update the
    charter + readiness, persist, and return the session. On a model/parse failure
    the turn degrades gracefully (a retry-able PM reply; charter unchanged)."""
    msg = user_message.strip()
    if msg:
        session.messages.append({"role": "user", "text": msg})
    call = caller or _default_caller()
    prompt = build_prompt(session, context=context)
    try:
        raw = call(_synthetic_member(session.model_route), prompt) or ""
    except Exception as exc:  # noqa: BLE001 — never surface a raw egress error
        session.messages.append({
            "role": "pm",
            "text": "I couldn't reach the model just now — try again in a moment.",
        })
        _save(session)
        raise WizardError("wizard_model_unreachable") from exc

    parsed = _extract_json(raw)
    if parsed is None:
        # Degrade: treat the whole reply as conversational, no charter change.
        reply = raw.strip() or "(no reply)"
        session.messages.append({"role": "pm", "text": reply})
        session.ready = False
        session.missing = _compute_missing(session.charter)
        _save(session)
        return session

    reply = str(parsed.get("reply") or "").strip() or "(no reply)"
    new_charter = parsed.get("charter")
    if isinstance(new_charter, dict):
        # keep prior values; only overwrite with non-empty new ones
        merged = dict(session.charter)
        for k, v in new_charter.items():
            if isinstance(v, (str, bool)) and (v != "" if isinstance(v, str) else True):
                merged[k] = v
        session.charter = merged
    session.missing = _compute_missing(session.charter)
    # The model's own "ready" is only honored when the required fields are truly
    # present — the completion contract is enforced here, not trusted to the model.
    session.ready = not session.missing and bool(parsed.get("ready"))
    session.messages.append({"role": "pm", "text": reply})
    _save(session)
    return session


def finalize(session: WizardSession) -> dict[str, Any]:
    """Return the runnable charter, or raise ``WizardError`` listing what's missing
    (the runnable-by-construction gate — grounded-or-refuse for goals)."""
    missing = _compute_missing(session.charter)
    if missing or not session.ready:
        raise WizardError(f"charter_incomplete: {', '.join(missing) or 'not ready'}")
    modality = str(session.charter.get("modality") or "").strip().lower()
    if modality not in MODALITIES:
        raise WizardError(f"invalid_modality: {modality!r}")
    return {
        "north_star": str(session.charter.get("north_star") or "").strip(),
        "definition_of_done": str(session.charter.get("definition_of_done") or "").strip(),
        "audience": str(session.charter.get("audience") or "").strip(),
        "modality": modality,
        "entrypoint": str(session.charter.get("entrypoint") or "").strip(),
        "scope_notes": str(session.charter.get("scope_notes") or "").strip(),
        "team_recipe": str(session.charter.get("team_recipe") or "balanced").strip(),
        "autonomous": _as_bool(session.charter.get("autonomous")),
    }


__all__ = [
    "WizardSession", "WizardError", "OPENING", "MODALITIES",
    "REQUIRED_CHARTER_FIELDS", "new_session", "get_session", "discard_session",
    "build_prompt", "run_turn", "finalize",
]
