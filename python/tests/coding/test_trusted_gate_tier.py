from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from errorta_council.coding.ledger import LedgerStore


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
