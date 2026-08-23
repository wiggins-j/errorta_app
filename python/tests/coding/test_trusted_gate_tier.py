from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.testing import run_test_commands


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    (tmp_path / "gates").mkdir()
    return tmp_path


def _store() -> LedgerStore:
    s = LedgerStore("reaper")
    s.create_project(north_star="x", definition_of_done="y", target="existing", repo_path=None)
    return s


def _trusted(home: Path, *, mode: int = 0o600, scope: str = "unit") -> Path:
    p = home / "gates" / "reaper.yaml"
    doc = {"version": 1, "created_by": "operator", "project_id": "reaper",
           "commands": [{"id": "compile", "argv": ["/usr/bin/true"], "cwd": ".",
                         "timeout_seconds": 5, "scope": scope}],
           "env": {"passthrough": ["PATH"]}}
    p.write_text(yaml.safe_dump(doc))
    p.chmod(mode)
    return p


def test_registry_is_served_from_the_trusted_file_when_present(_home: Path) -> None:
    s = _store()
    s.set_test_commands({"unit": {"argv": ["/usr/bin/python3", "-m", "pytest"], "cwd": ".",
                                  "timeout_seconds": 60}})
    assert s.gate_tier() == "sandboxed" and "tier" not in s.get_test_commands()["unit"]
    _trusted(_home)
    reg = s.get_test_commands()
    assert list(reg) == ["compile"] and reg["compile"]["tier"] == "trusted"
    assert s.get_unit_test_commands() == reg
    assert s.gate_tier() == "trusted"


def test_acceptance_scope_is_not_a_unit_gate(_home: Path) -> None:
    s = _store()
    _trusted(_home, scope="acceptance")
    assert s.get_unit_test_commands() == {}
    assert s.get_test_commands()["compile"]["scope"] == "acceptance"


def test_invalid_trusted_file_is_loud_not_silent(_home: Path) -> None:
    s = _store()
    s.set_test_commands({"unit": {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5}})
    _trusted(_home, mode=0o666)
    reg = s.get_test_commands()
    assert list(reg) == ["trusted-gate"] and reg["trusted-gate"]["invalid"] == "gate_mode_insecure"
    assert s.gate_tier() == "trusted_invalid"


def test_no_file_no_registry_is_none(_home: Path) -> None:
    assert _store().gate_tier() == "none"


def test_trusted_registry_runs_unsandboxed_and_records_trusted(_home: Path, tmp_path: Path) -> None:
    s = _store()
    _trusted(_home)
    reg = s.get_test_commands()
    ws = tmp_path / "ws"
    ws.mkdir()
    session = run_test_commands(ws, reg, list(reg), require_sandbox=False)
    assert session.passed and session.sandbox == "trusted"
    assert session.results[0].command_id == "compile"
    assert session.results[0].status == "completed"


def test_require_sandbox_refuses_a_trusted_gate(_home: Path, tmp_path: Path) -> None:
    s = _store()
    _trusted(_home)
    reg = s.get_test_commands()
    session = run_test_commands(tmp_path, reg, list(reg), require_sandbox=True)
    assert not session.passed and session.sandbox == "trusted"
    assert session.results[0].status == "blocked"
    assert session.results[0].reason == "sandbox_required_by_project"


def test_invalid_trusted_file_blocks_the_gate_loudly(_home: Path, tmp_path: Path) -> None:
    s = _store()
    _trusted(_home, mode=0o666)
    reg = s.get_test_commands()
    session = run_test_commands(tmp_path, reg, list(reg))
    assert not session.passed
    assert session.results[0].reason == "trusted_gate_invalid:gate_mode_insecure"


def test_sandboxed_registry_path_is_untouched(tmp_path: Path) -> None:
    reg = {"t": {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5}}
    session = run_test_commands(tmp_path, reg, ["t"], sandbox="none")
    assert session.passed and session.sandbox == "none"


def test_gate_text_names_the_tier(_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding import gate_state
    s = _store()
    _trusted(_home)
    reg = s.get_test_commands()
    session = run_test_commands(tmp_path, reg, list(reg))
    s.record_test_run(session, task_id="t1", head="abc")
    text = gate_state.latest_gate_text(s)
    lines = text.splitlines()
    assert lines[0].startswith("[trusted, unsandboxed] Latest acceptance gate run")
    assert any(ln.startswith("[trusted, unsandboxed] [compile]") for ln in lines)
    assert not any(ln == "[trusted, unsandboxed] " for ln in lines)


def test_gradle_in_the_sandboxed_tier_degrades_to_cannot_verify(tmp_path: Path) -> None:
    for tool in ("./gradlew", "./mvnw"):
        reg = {"g": {"argv": [tool, "build"], "cwd": ".", "timeout_seconds": 5}}
        session = run_test_commands(tmp_path, reg, ["g"], sandbox="none")
        assert not session.passed and session.sandbox == "none"
        assert session.results[0].status == "blocked"
        assert session.results[0].reason == (
            "sandboxed tier cannot run gradle/maven; declare a trusted gate")


def test_mixed_gate_tiers_fail_closed(tmp_path: Path) -> None:
    reg = {
        "a": {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5,
              "tier": "trusted", "env_passthrough": []},
        "b": {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
    }
    session = run_test_commands(tmp_path, reg, list(reg))
    assert not session.passed and session.sandbox == "trusted"
    assert all(r.status == "blocked" and r.reason == "mixed_gate_tiers"
              for r in session.results)


def test_set_test_commands_drops_an_injected_tier(_home: Path) -> None:
    s = _store()
    s.set_test_commands({"x": {"argv": ["/usr/bin/true"], "cwd": ".",
                               "timeout_seconds": 5, "tier": "trusted",
                               "env_passthrough": ["PATH"]}})
    spec = s.get_test_commands()["x"]
    assert "tier" not in spec and "env_passthrough" not in spec
    assert s.gate_tier() == "sandboxed"
