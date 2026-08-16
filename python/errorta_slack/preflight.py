"""Live-test infra: a read-only "doctor" for the Slack PM bridge.

De-risks the one path the rest of the test suite cannot reach without a
real Slack workspace: the client construction inside
``errorta_app.slack_lifecycle.sync()`` (``WebClient(token=bot_token)`` /
``SocketModeClient(app_token=app_token, web_client=...)``). This module
validates the same config/secrets/store state ``sync()`` gates on, and,
with ``--connect``, performs a real (but side-effect-free — it posts
nothing) connectivity probe using the same object shapes.

Every check is read-only. ``run_checks`` takes its collaborators through
``PreflightDeps`` so the whole thing is unit-testable with fakes and never
touches the network unless ``connect=True`` AND the caller supplies (or
lets this module default to) real ``slack_sdk`` clients.

This module MUST NOT import ``slack_sdk`` at module load time — mirrors
``errorta_slack.config``/``secrets``/``store``'s optionality discipline.
The only place ``slack_sdk`` is imported is lazily, inside the
``slack-sdk installed`` check and inside the default ``--connect``
client factories, so ``import errorta_slack.preflight`` and
``python -m errorta_slack.preflight`` (in "not enabled yet" mode) never
require the optional ``slack-sdk`` extra to be installed to report that
fact usefully.

Security note: never print or log a raw token. Checks that need to show
something about a token use ``secrets.mask()`` (``"…<last4>"``); the
``--connect`` checks surface only team/bot names from Slack's own
``auth.test`` response, never a token value.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal

Status = Literal["ok", "warn", "fail"]


@dataclass
class CheckResult:
    """One preflight check's outcome. ``detail`` is always safe to print —
    checks are responsible for never putting a raw token in it."""

    name: str
    status: Status
    detail: str = ""


@dataclass
class PreflightDeps:
    """Every seam a check reaches through — all injectable so tests run
    network-free with fakes.

    ``web_client_factory``/``socket_client_factory`` are ``None`` by
    default; the ``--connect`` checks lazily default them to real
    ``slack_sdk`` constructors only when they're actually needed, so
    constructing ``PreflightDeps`` (and every non-``--connect`` check)
    never requires ``slack_sdk`` to be installed.
    """

    config_mod: Any
    secrets_mod: Any
    store_mod: Any
    ledger_factory: Callable[[str], Any]
    web_client_factory: Callable[[str], Any] | None = None
    socket_client_factory: Callable[[str, Any], Any] | None = None


# --------------------------------------------------------------------------
# Default (real) slack_sdk client factories — imported lazily, only reached
# from the --connect checks below when the caller didn't inject its own.
# --------------------------------------------------------------------------


def _default_web_client_factory(bot_token: str) -> Any:
    from slack_sdk import WebClient

    return WebClient(token=bot_token)


def _default_socket_client_factory(app_token: str, web_client: Any) -> Any:
    from slack_sdk.socket_mode import SocketModeClient

    return SocketModeClient(app_token=app_token, web_client=web_client)


# --------------------------------------------------------------------------
# Checks 1-6: static, offline validation of config/secrets/store state.
# --------------------------------------------------------------------------


def _check_sdk_installed() -> CheckResult:
    try:
        import slack_sdk  # noqa: F401
    except ImportError as exc:
        return CheckResult(
            "slack-sdk installed",
            "fail",
            f"slack_sdk not importable ({exc}); install the optional extra: "
            "pip install errorta-app[slack]",
        )
    return CheckResult("slack-sdk installed", "ok", "slack_sdk import succeeded")


def _check_enabled(deps: PreflightDeps) -> CheckResult:
    if bool(deps.config_mod.is_enabled()):
        return CheckResult("bridge enabled", "ok", "config.is_enabled() is True")
    return CheckResult(
        "bridge enabled",
        "fail",
        "bridge is disabled; enable it (POST /slack/enable, or "
        "config.save({'enabled': True})) before going live",
    )


def _check_tokens(deps: PreflightDeps, tokens: dict[str, str] | None) -> CheckResult:
    masked = deps.secrets_mod.mask()
    if not tokens:
        return CheckResult(
            "tokens present + shape",
            "fail",
            f"no tokens on disk (masked: {masked}); run the Slack token setup flow",
        )
    app_token = tokens.get("app_token") or ""
    bot_token = tokens.get("bot_token") or ""
    problems = []
    if not app_token.startswith("xapp-"):
        problems.append("app_token does not start with 'xapp-'")
    if not bot_token.startswith("xoxb-"):
        problems.append("bot_token does not start with 'xoxb-'")
    if problems:
        return CheckResult(
            "tokens present + shape",
            "fail",
            f"{'; '.join(problems)} (masked: {masked})",
        )
    return CheckResult(
        "tokens present + shape", "ok", f"tokens present and well-shaped (masked: {masked})"
    )


def _check_allowlist(cfg: dict[str, Any]) -> CheckResult:
    team_ids = cfg.get("allowed_team_ids") or []
    user_ids = cfg.get("allowed_user_ids") or []
    if team_ids and user_ids:
        return CheckResult(
            "allowlist populated",
            "ok",
            f"{len(team_ids)} team id(s), {len(user_ids)} user id(s) allowed",
        )
    empty = [
        name
        for name, value in (("allowed_team_ids", team_ids), ("allowed_user_ids", user_ids))
        if not value
    ]
    return CheckResult(
        "allowlist populated",
        "fail",
        f"{' and '.join(empty)} empty — an empty allowlist denies everyone "
        "(auth is fail-closed); populate before going live",
    )


def _check_channel_linked(bindings: list[dict[str, Any]]) -> CheckResult:
    if bindings:
        return CheckResult("channel linked", "ok", f"{len(bindings)} channel binding(s) found")
    return CheckResult(
        "channel linked", "fail", "no channel bound; bind a channel to a project before going live"
    )


def _check_bound_projects(deps: PreflightDeps, bindings: list[dict[str, Any]]) -> CheckResult:
    if not bindings:
        return CheckResult("bound projects exist", "ok", "no channel bindings to check")
    unresolved: list[str] = []
    for binding in bindings:
        project_id = str(binding.get("project_id"))
        try:
            store = deps.ledger_factory(project_id)
            store.get_project()
        except Exception:  # noqa: BLE001 - "can't be determined" -> warn, not fail
            unresolved.append(project_id)
    if unresolved:
        return CheckResult(
            "bound projects exist",
            "warn",
            "could not confirm project(s) exist: " + ", ".join(unresolved),
        )
    return CheckResult(
        "bound projects exist", "ok", f"confirmed {len(bindings)} bound project(s)"
    )


# --------------------------------------------------------------------------
# Checks 7-8: --connect only. Real (but posting-nothing) connectivity probe.
# --------------------------------------------------------------------------


def _check_auth_test(deps: PreflightDeps, tokens: dict[str, str] | None) -> CheckResult:
    bot_token = (tokens or {}).get("bot_token")
    if not bot_token:
        return CheckResult("auth.test", "fail", "no bot_token available; cannot probe")
    factory = deps.web_client_factory or _default_web_client_factory
    try:
        client = factory(bot_token)
        resp = client.auth_test()
        if not resp["ok"]:
            error = resp.get("error") if hasattr(resp, "get") else None
            return CheckResult("auth.test", "fail", f"auth.test returned not-ok: {error or resp}")
        team = resp.get("team") or "?"
        bot_user = resp.get("user") or "?"
        return CheckResult("auth.test", "ok", f"connected as {bot_user} on team {team}")
    except Exception as exc:  # noqa: BLE001 - degrade to a fail CheckResult, never crash
        return CheckResult("auth.test", "fail", f"auth.test failed: {exc}")


def _check_socket_connect(deps: PreflightDeps, tokens: dict[str, str] | None) -> CheckResult:
    app_token = (tokens or {}).get("app_token")
    bot_token = (tokens or {}).get("bot_token")
    if not app_token or not bot_token:
        return CheckResult("socket mode connect", "fail", "tokens missing; cannot probe")
    web_factory = deps.web_client_factory or _default_web_client_factory
    socket_factory = deps.socket_client_factory or _default_socket_client_factory
    try:
        web_client = web_factory(bot_token)
        client = socket_factory(app_token, web_client)
        client.connect()
        client.disconnect()
    except Exception as exc:  # noqa: BLE001 - degrade to a fail CheckResult, never crash
        return CheckResult("socket mode connect", "fail", f"socket mode connect failed: {exc}")
    return CheckResult("socket mode connect", "ok", "connected and disconnected cleanly")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_checks(*, connect: bool = False, deps: PreflightDeps) -> list[CheckResult]:
    """Run every preflight check and return their results in order.

    Always runs checks 1-6 (offline). With ``connect=True`` also runs
    checks 7-8 (a real connectivity probe via the injected/defaulted
    ``slack_sdk`` client factories). Never raises — every check catches
    its own exceptions and degrades to a ``fail`` ``CheckResult``.
    """
    results: list[CheckResult] = [_check_sdk_installed(), _check_enabled(deps)]

    tokens = deps.secrets_mod.load_tokens()
    results.append(_check_tokens(deps, tokens))

    cfg = deps.config_mod.load()
    results.append(_check_allowlist(cfg))

    bindings = deps.store_mod.list_bindings()
    results.append(_check_channel_linked(bindings))
    results.append(_check_bound_projects(deps, bindings))

    if connect:
        results.append(_check_auth_test(deps, tokens))
        results.append(_check_socket_connect(deps, tokens))

    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

_ICONS: dict[Status, str] = {"ok": "✅", "warn": "⚠️", "fail": "❌"}


def _format_line(result: CheckResult) -> str:
    icon = _ICONS[result.status]
    if result.detail:
        return f"{icon} {result.name}: {result.detail}"
    return f"{icon} {result.name}"


def _real_ledger_factory() -> Callable[[str], Any]:
    """Indirection point so tests can substitute the real
    ``LedgerStore`` without touching a real ledger directory."""
    from errorta_council.coding.ledger import LedgerStore

    return LedgerStore


def _build_real_deps() -> PreflightDeps:
    from errorta_slack import config as config_mod
    from errorta_slack import secrets as secrets_mod
    from errorta_slack import store as store_mod

    return PreflightDeps(
        config_mod=config_mod,
        secrets_mod=secrets_mod,
        store_mod=store_mod,
        ledger_factory=_real_ledger_factory(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m errorta_slack.preflight",
        description="Read-only doctor for the Slack PM bridge live-test setup.",
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Also run a real, side-effect-free connectivity probe "
        "(auth.test + Socket Mode connect/disconnect; posts nothing).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of a checklist."
    )
    args = parser.parse_args(argv)

    deps = _build_real_deps()
    results = run_checks(connect=args.connect, deps=deps)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for result in results:
            print(_format_line(result))

    return 0 if all(r.status != "fail" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
