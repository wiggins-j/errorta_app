"""Live-test infra: a read-only "doctor" for the Slack PM bridge.

De-risks the one untested path (the real ``slack_lifecycle.sync()`` client
construction — ``WebClient``/``SocketModeClient``) by giving an operator a
tool that validates config/secrets/store state and, with ``--connect``,
does a real (but side-effect-free) connectivity probe using the SAME
object shapes ``slack_lifecycle`` builds.

Every test here injects fakes via ``preflight.PreflightDeps`` — no network,
no real Slack tokens (this is a PUBLIC repo; placeholder shapes only).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from errorta_slack import preflight

# Placeholder tokens only — this is a public repo. Never a real Slack token.
APP_TOKEN = "xapp-1-AAA-bbb"
BOT_TOKEN = "xoxb-9-cccddd"


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# --- fakes -------------------------------------------------------------


def _mask(tokens: dict[str, str] | None) -> dict[str, Any]:
    if not tokens:
        return {"app_token": None, "bot_token": None}

    def m(v: str | None) -> str | None:
        return None if not v else "…" + v[-4:]

    return {"app_token": m(tokens.get("app_token")), "bot_token": m(tokens.get("bot_token"))}


def make_config_mod(
    *,
    enabled: bool = True,
    allowed_team_ids: list[str] | None = None,
    allowed_user_ids: list[str] | None = None,
) -> Any:
    cfg = {
        "enabled": enabled,
        "allowed_team_ids": list(allowed_team_ids or []),
        "allowed_user_ids": list(allowed_user_ids or []),
    }
    return SimpleNamespace(load=lambda: dict(cfg), is_enabled=lambda: enabled)


def make_secrets_mod(tokens: dict[str, str] | None) -> Any:
    return SimpleNamespace(load_tokens=lambda: tokens, mask=lambda: _mask(tokens))


def make_store_mod(bindings: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(list_bindings=lambda: list(bindings))


class _FakeLedgerOk:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def get_project(self) -> dict[str, Any]:
        return {"id": self.project_id}


class _FakeLedgerMissing:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def get_project(self) -> Any:
        raise RuntimeError(f"no such project: {self.project_id}")


class _FakeWebClient:
    """Records that it was built with a bot token; never sees a network."""

    def __init__(self, bot_token: str | None) -> None:
        self.bot_token = bot_token

    def auth_test(self) -> dict[str, Any]:
        return {"ok": True, "team": "Errorta Test Team", "user": "errorta-pm"}


class _FailingWebClient:
    """Simulates a careless SDK/network error that echoes the raw token it
    was trying to use back in the exception message — exactly the shape
    of leak the ``auth.test`` check must never let reach output."""

    def __init__(self, bot_token: str | None) -> None:
        self.bot_token = bot_token

    def auth_test(self) -> dict[str, Any]:
        raise RuntimeError(f"invalid_auth (token used: {self.bot_token})")


class _FakeSocketClient:
    def __init__(self, app_token: str | None, web_client: Any) -> None:
        self.app_token = app_token
        self.web_client = web_client
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")

    def disconnect(self) -> None:
        self.calls.append("disconnect")


class _FailingSocketClient:
    """Simulates a careless SDK/network error that echoes both raw tokens
    it was trying to use back in the exception message."""

    def __init__(self, app_token: str | None, web_client: Any) -> None:
        self.app_token = app_token
        self.bot_token = getattr(web_client, "bot_token", None)

    def connect(self) -> None:
        raise RuntimeError(
            f"socket connect failed: timeout (app_token={self.app_token}, "
            f"bot_token={self.bot_token})"
        )

    def disconnect(self) -> None:  # pragma: no cover - never reached
        raise AssertionError("disconnect should not be called after a failed connect")


_UNSET = object()


def base_deps(
    *,
    enabled: bool = True,
    allowed_team_ids: list[str] | None = None,
    allowed_user_ids: list[str] | None = None,
    tokens: Any = _UNSET,
    bindings: list[dict[str, Any]] | None = None,
    ledger_factory: Any = _FakeLedgerOk,
    web_client_factory: Any = None,
    socket_client_factory: Any = None,
) -> preflight.PreflightDeps:
    if tokens is _UNSET:
        tokens = {"app_token": APP_TOKEN, "bot_token": BOT_TOKEN}
    if allowed_team_ids is None:
        allowed_team_ids = ["T123"]
    if allowed_user_ids is None:
        allowed_user_ids = ["U123"]
    if bindings is None:
        bindings = [{"channel_id": "C1", "project_id": "proj-1"}]
    return preflight.PreflightDeps(
        config_mod=make_config_mod(
            enabled=enabled,
            allowed_team_ids=allowed_team_ids,
            allowed_user_ids=allowed_user_ids,
        ),
        secrets_mod=make_secrets_mod(tokens),
        store_mod=make_store_mod(bindings),
        ledger_factory=ledger_factory,
        web_client_factory=web_client_factory,
        socket_client_factory=socket_client_factory,
    )


def by_name(results: list[preflight.CheckResult], name: str) -> preflight.CheckResult:
    for r in results:
        if r.name == name:
            return r
    raise AssertionError(f"no check named {name!r} in {[r.name for r in results]}")


# --- CheckResult / PreflightDeps shape ----------------------------------


def test_check_result_is_a_plain_dataclass_with_expected_fields() -> None:
    r = preflight.CheckResult(name="x", status="ok", detail="fine")
    assert r.name == "x"
    assert r.status == "ok"
    assert r.detail == "fine"


# --- check 1: slack-sdk installed ---------------------------------------


def test_slack_sdk_installed_is_ok_when_importable() -> None:
    results = preflight.run_checks(deps=base_deps())
    r = by_name(results, "slack-sdk installed")
    assert r.status == "ok"


class _BlockSlackSdk:
    """Mirrors tests/slack/test_optionality.py's meta_path blocker."""

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        if name == "slack_sdk" or name.startswith("slack_sdk."):
            raise ImportError(f"blocked for test: {name}")
        return None


def test_slack_sdk_installed_is_fail_when_unimportable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "slack_sdk" or name.startswith("slack_sdk."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    blocker = _BlockSlackSdk()
    sys.meta_path.insert(0, blocker)
    try:
        results = preflight.run_checks(deps=base_deps())
    finally:
        sys.meta_path.remove(blocker)

    r = by_name(results, "slack-sdk installed")
    assert r.status == "fail"


# --- check 2: bridge enabled ---------------------------------------------


def test_bridge_enabled_ok_when_enabled() -> None:
    results = preflight.run_checks(deps=base_deps(enabled=True))
    assert by_name(results, "bridge enabled").status == "ok"


def test_bridge_enabled_fail_when_disabled() -> None:
    results = preflight.run_checks(deps=base_deps(enabled=False))
    assert by_name(results, "bridge enabled").status == "fail"


# --- check 3: tokens present + shape --------------------------------------


def test_tokens_ok_when_present_and_well_shaped() -> None:
    results = preflight.run_checks(deps=base_deps())
    assert by_name(results, "tokens present + shape").status == "ok"


def test_tokens_fail_when_absent() -> None:
    results = preflight.run_checks(deps=base_deps(tokens=None))
    assert by_name(results, "tokens present + shape").status == "fail"


def test_tokens_fail_when_wrong_shape() -> None:
    results = preflight.run_checks(
        deps=base_deps(tokens={"app_token": "not-an-app-token", "bot_token": BOT_TOKEN})
    )
    assert by_name(results, "tokens present + shape").status == "fail"

    results = preflight.run_checks(
        deps=base_deps(tokens={"app_token": APP_TOKEN, "bot_token": "not-a-bot-token"})
    )
    assert by_name(results, "tokens present + shape").status == "fail"


def test_tokens_check_never_prints_raw_token_value() -> None:
    results = preflight.run_checks(deps=base_deps())
    text = json.dumps([r.__dict__ for r in results])
    assert APP_TOKEN not in text
    assert BOT_TOKEN not in text


# --- check 4: allowlist populated -----------------------------------------


def test_allowlist_ok_when_both_populated() -> None:
    results = preflight.run_checks(
        deps=base_deps(allowed_team_ids=["T1"], allowed_user_ids=["U1"])
    )
    assert by_name(results, "allowlist populated").status == "ok"


def test_allowlist_fail_when_team_ids_empty() -> None:
    results = preflight.run_checks(deps=base_deps(allowed_team_ids=[], allowed_user_ids=["U1"]))
    r = by_name(results, "allowlist populated")
    assert r.status == "fail"
    assert "deny" in r.detail.lower() or "empty" in r.detail.lower()


def test_allowlist_fail_when_user_ids_empty() -> None:
    results = preflight.run_checks(deps=base_deps(allowed_team_ids=["T1"], allowed_user_ids=[]))
    assert by_name(results, "allowlist populated").status == "fail"


def test_allowlist_fail_when_both_empty() -> None:
    results = preflight.run_checks(deps=base_deps(allowed_team_ids=[], allowed_user_ids=[]))
    assert by_name(results, "allowlist populated").status == "fail"


# --- check 5: channel linked -----------------------------------------------


def test_channel_linked_ok_when_bindings_present() -> None:
    results = preflight.run_checks(
        deps=base_deps(bindings=[{"channel_id": "C1", "project_id": "proj-1"}])
    )
    assert by_name(results, "channel linked").status == "ok"


def test_channel_linked_fail_when_no_bindings() -> None:
    results = preflight.run_checks(deps=base_deps(bindings=[]))
    assert by_name(results, "channel linked").status == "fail"


# --- check 6: bound projects exist -----------------------------------------


def test_bound_projects_ok_when_ledger_resolves_all() -> None:
    results = preflight.run_checks(
        deps=base_deps(
            bindings=[{"channel_id": "C1", "project_id": "proj-1"}],
            ledger_factory=_FakeLedgerOk,
        )
    )
    assert by_name(results, "bound projects exist").status == "ok"


def test_bound_projects_ok_when_no_bindings_to_check() -> None:
    results = preflight.run_checks(deps=base_deps(bindings=[]))
    assert by_name(results, "bound projects exist").status == "ok"


def test_bound_projects_warn_not_fail_when_project_missing() -> None:
    results = preflight.run_checks(
        deps=base_deps(
            bindings=[{"channel_id": "C1", "project_id": "proj-missing"}],
            ledger_factory=_FakeLedgerMissing,
        )
    )
    r = by_name(results, "bound projects exist")
    assert r.status == "warn"


def test_bound_projects_warn_when_ledger_factory_itself_raises() -> None:
    def _boom(project_id: str) -> Any:
        raise RuntimeError("cannot construct ledger store")

    results = preflight.run_checks(
        deps=base_deps(
            bindings=[{"channel_id": "C1", "project_id": "proj-1"}],
            ledger_factory=_boom,
        )
    )
    r = by_name(results, "bound projects exist")
    assert r.status == "warn"


# --- connect=False never adds connect checks -------------------------------


def test_connect_checks_absent_when_connect_false() -> None:
    results = preflight.run_checks(connect=False, deps=base_deps())
    names = [r.name for r in results]
    assert "auth.test" not in names
    assert "socket mode connect" not in names
    assert len(results) == 6


# --- check 7: auth.test (connect=True) -------------------------------------


def test_auth_test_ok_uses_injected_web_client_factory() -> None:
    results = preflight.run_checks(
        connect=True, deps=base_deps(web_client_factory=_FakeWebClient)
    )
    r = by_name(results, "auth.test")
    assert r.status == "ok"
    assert "errorta-pm" in r.detail or "Errorta Test Team" in r.detail


def test_auth_test_fail_on_exception_not_crash() -> None:
    """Also proves the hardening from Item 1: the raising client's
    exception message deliberately embeds the raw bot token (simulating a
    careless SDK/network error) — the resulting CheckResult.detail must
    degrade to ``fail`` AND must never let that raw token through."""
    results = preflight.run_checks(
        connect=True, deps=base_deps(web_client_factory=_FailingWebClient)
    )
    r = by_name(results, "auth.test")
    assert r.status == "fail"
    assert "invalid_auth" in r.detail
    assert BOT_TOKEN not in r.detail


def test_auth_test_fail_when_response_not_ok() -> None:
    def factory(bot_token: str | None) -> Any:
        return SimpleNamespace(auth_test=lambda: {"ok": False, "error": "invalid_auth"})

    results = preflight.run_checks(connect=True, deps=base_deps(web_client_factory=factory))
    r = by_name(results, "auth.test")
    assert r.status == "fail"


def test_auth_test_detail_never_contains_raw_bot_token() -> None:
    results = preflight.run_checks(
        connect=True, deps=base_deps(web_client_factory=_FakeWebClient)
    )
    text = json.dumps([r.__dict__ for r in results])
    assert BOT_TOKEN not in text
    assert APP_TOKEN not in text


# --- check 8: socket mode connect (connect=True) ----------------------------


def test_socket_connect_ok_records_connect_and_disconnect() -> None:
    seen: dict[str, Any] = {}

    def socket_factory(app_token: str | None, web_client: Any) -> Any:
        client = _FakeSocketClient(app_token, web_client)
        seen["client"] = client
        return client

    results = preflight.run_checks(
        connect=True,
        deps=base_deps(web_client_factory=_FakeWebClient, socket_client_factory=socket_factory),
    )
    r = by_name(results, "socket mode connect")
    assert r.status == "ok"
    assert seen["client"].calls == ["connect", "disconnect"]


def test_socket_connect_fail_on_exception_not_crash() -> None:
    """Also proves the hardening from Item 1: the raising client's
    exception message deliberately embeds both raw tokens (simulating a
    careless SDK/network error) — the resulting CheckResult.detail must
    degrade to ``fail`` AND must never let either raw token through."""
    results = preflight.run_checks(
        connect=True,
        deps=base_deps(
            web_client_factory=_FakeWebClient, socket_client_factory=_FailingSocketClient
        ),
    )
    r = by_name(results, "socket mode connect")
    assert r.status == "fail"
    assert "timeout" in r.detail
    assert APP_TOKEN not in r.detail
    assert BOT_TOKEN not in r.detail


def test_connect_true_produces_eight_results_total() -> None:
    results = preflight.run_checks(
        connect=True,
        deps=base_deps(
            web_client_factory=_FakeWebClient, socket_client_factory=_FakeSocketClient
        ),
    )
    assert len(results) == 8


# --- main() ------------------------------------------------------------


def test_main_returns_zero_when_all_checks_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from errorta_slack import config as real_config
    from errorta_slack import secrets as real_secrets
    from errorta_slack import store as real_store

    real_config.save(
        {"enabled": True, "allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]}
    )
    real_secrets.save_tokens(APP_TOKEN, BOT_TOKEN)
    real_store.bind_channel("C1", "proj-1")

    def fake_ledger_factory(project_id: str) -> Any:
        return _FakeLedgerOk(project_id)

    monkeypatch.setattr(preflight, "_real_ledger_factory", lambda: fake_ledger_factory)

    rc = preflight.main([])

    out = capsys.readouterr().out
    assert rc == 0
    assert "channel linked" in out
    assert APP_TOKEN not in out
    assert BOT_TOKEN not in out


def test_main_connect_flag_wires_through_to_connect_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Drives the CLI entrypoint with --connect end-to-end, exercising the
    real argparse -> run_checks(connect=True) wiring inside main() itself
    (not just run_checks() called directly, as the other connect tests
    do). Injects fakes through the same _real_*_factory indirection
    points main()'s real-deps builder uses, so no network is touched."""
    from errorta_slack import config as real_config
    from errorta_slack import secrets as real_secrets
    from errorta_slack import store as real_store

    real_config.save(
        {"enabled": True, "allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]}
    )
    real_secrets.save_tokens(APP_TOKEN, BOT_TOKEN)
    real_store.bind_channel("C1", "proj-1")

    monkeypatch.setattr(preflight, "_real_ledger_factory", lambda: _FakeLedgerOk)
    monkeypatch.setattr(preflight, "_real_web_client_factory", lambda: _FakeWebClient)

    def socket_factory(app_token: str | None, web_client: Any) -> Any:
        return _FakeSocketClient(app_token, web_client)

    monkeypatch.setattr(preflight, "_real_socket_client_factory", lambda: socket_factory)

    rc = preflight.main(["--connect", "--json"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    names = [item["name"] for item in payload]

    assert rc == 0
    assert len(payload) == 8
    assert "auth.test" in names
    assert "socket mode connect" in names
    assert all(item["status"] != "fail" for item in payload)
    assert APP_TOKEN not in out
    assert BOT_TOKEN not in out


def test_main_exit_code_zero_when_only_warn_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Warns don't fail the exit code: every other gate is satisfied, but
    the bound project can't be confirmed (check 6 -> warn) -- main() must
    still report success (0)."""
    from errorta_slack import config as real_config
    from errorta_slack import secrets as real_secrets
    from errorta_slack import store as real_store

    real_config.save(
        {"enabled": True, "allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]}
    )
    real_secrets.save_tokens(APP_TOKEN, BOT_TOKEN)
    real_store.bind_channel("C1", "proj-missing")

    monkeypatch.setattr(preflight, "_real_ledger_factory", lambda: _FakeLedgerMissing)

    rc = preflight.main(["--json"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    statuses = {item["status"] for item in payload}

    assert "fail" not in statuses
    assert "warn" in statuses
    assert rc == 0


def test_main_returns_nonzero_on_unconfigured_home(capsys: pytest.CaptureFixture) -> None:
    rc = preflight.main([])
    out = capsys.readouterr().out
    assert rc != 0
    assert out  # printed a human checklist


def test_main_json_flag_emits_parseable_json(capsys: pytest.CaptureFixture) -> None:
    rc = preflight.main(["--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert all({"name", "status", "detail"} <= set(item.keys()) for item in payload)
    assert rc != 0


def test_main_never_prints_raw_tokens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from errorta_slack import config as real_config
    from errorta_slack import secrets as real_secrets

    real_config.save(
        {"enabled": True, "allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]}
    )
    real_secrets.save_tokens(APP_TOKEN, BOT_TOKEN)

    preflight.main(["--json"])
    out = capsys.readouterr().out
    assert APP_TOKEN not in out
    assert BOT_TOKEN not in out


def test_main_module_entrypoint_runs_without_crashing() -> None:
    """``python -m errorta_slack.preflight --json`` smoke test — must not
    import slack_sdk unexpectedly, must not crash, and reports fails (exit
    non-zero) on an unconfigured machine, which is the CORRECT behavior."""
    env = {**__import__("os").environ}
    proc = subprocess.run(
        [sys.executable, "-m", "errorta_slack.preflight", "--json"],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(proc.stdout)
    assert isinstance(payload, list)
    assert proc.returncode != 0
