"""Persistent configuration for the Slack PM bridge.

Mirrors `errorta_mobile/config.py`'s atomic-write + `${ERRORTA_HOME}`
resolution pattern. This module MUST NOT import `slack_sdk` or any other
optional dependency at module load time — the Slack bridge is strictly
optional and disabled by default, so the rest of the sidecar must keep
working when `slack-sdk` isn't installed.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from errorta_app.paths import errorta_home

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "bindings": [],
    "window": 20,
    "timeout_minutes": 30,
    # Carried from Task 7: `auth.is_allowed` reads these two keys and is
    # fail-closed when either is empty/missing (denies everyone) — that is
    # never overridden here. An empty default list is therefore the safe,
    # deny-by-default starting point, not an allow-all.
    "allowed_team_ids": [],
    "allowed_user_ids": [],
    # The studio manager (app-level PM) isn't tied to any one project, so it
    # can't borrow a project's PM route the way `concierge.run_turn` does --
    # it needs its own configured model route. `claude_cli.opus` is the
    # known-good default; see `studio_concierge.run_turn`'s `model_route`
    # kwarg and `connection._process_studio`, which reads this key.
    "studio_model_route": "claude_cli.opus",
    # fix/slack-studio-default-team: `create_project_from_charter` normally
    # resolves a team via `recipes.resolve_team(recipe, available_routes)`,
    # where `available_routes` defaults to the live
    # `pm_reference.list_available_routes()` -- on a machine where the
    # desktop app's Test probe hasn't marked claude_cli/cursor_cli
    # "connected", that returns only `custom.senditai`, so a studio-spun-up
    # project ends up with the wrong (or an empty) team and no working PM.
    # The studio instead builds an explicit `members=` team from this
    # config, bypassing resolve_team + the availability probe entirely.
    # Minimal role->route shape (not full member dicts) -- expanded into
    # canonical member dicts by `errorta_slack.studio_tools._default_team_members`
    # at use time.
    "studio_default_team": [
        {"coding_role": "pm", "gateway_route_id": "claude_cli.opus"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
        {"coding_role": "reviewer", "gateway_route_id": "claude_cli.sonnet"},
        {"coding_role": "tester", "gateway_route_id": "claude_cli.sonnet"},
    ],
}


def slack_dir() -> Path:
    p = errorta_home() / "slack"
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def config_path() -> Path:
    return slack_dir() / "config.json"


def _bool(value: Any) -> bool:
    return bool(value)


def _int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bindings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _str(value: Any, *, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return default


def _team_specs(value: Any) -> list[dict[str, str]]:
    default = [dict(spec) for spec in DEFAULT_CONFIG["studio_default_team"]]
    if not isinstance(value, list):
        return default
    specs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("coding_role")
        route = item.get("gateway_route_id")
        if (isinstance(role, str) and role.strip()
                and isinstance(route, str) and route.strip()):
            specs.append({"coding_role": role, "gateway_route_id": route})
    # A partial/empty result (e.g. every entry missing a field, or an empty
    # list) falls back to the default team rather than shipping a project
    # with a broken or empty team.
    return specs or default


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if raw:
        merged.update(raw)
    return {
        "enabled": _bool(merged.get("enabled")),
        "bindings": _bindings(merged.get("bindings")),
        "window": _int(merged.get("window"), default=int(DEFAULT_CONFIG["window"])),
        "timeout_minutes": _int(
            merged.get("timeout_minutes"),
            default=int(DEFAULT_CONFIG["timeout_minutes"]),
        ),
        "allowed_team_ids": _str_list(merged.get("allowed_team_ids")),
        "allowed_user_ids": _str_list(merged.get("allowed_user_ids")),
        "studio_model_route": _str(
            merged.get("studio_model_route"),
            default=str(DEFAULT_CONFIG["studio_model_route"]),
        ),
        "studio_default_team": _team_specs(merged.get("studio_default_team")),
    }


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        save(dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return dict(DEFAULT_CONFIG)
    return normalize(raw)


def save(cfg: dict[str, Any]) -> None:
    normalized = normalize(cfg)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-",
        suffix=".json",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def is_enabled() -> bool:
    return bool(load().get("enabled"))


__all__ = [
    "DEFAULT_CONFIG",
    "config_path",
    "is_enabled",
    "load",
    "normalize",
    "save",
    "slack_dir",
]
