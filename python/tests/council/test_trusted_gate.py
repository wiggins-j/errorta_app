from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from errorta_council.coding import trusted_gate as tg


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    (tmp_path / "gates").mkdir()
    return tmp_path


def _doc(**over) -> dict:
    d = {"version": 1, "created_by": "operator", "project_id": "reaper",
         "commands": [{"id": "compile", "argv": ["./gradlew", ":client:compileJava", "--offline"],
                       "cwd": ".", "timeout_seconds": 900, "scope": "unit"}],
         "env": {"passthrough": ["PATH", "HOME", "JAVA_HOME"]}}
    d.update(over)
    return d


def _write(home: Path, doc: dict, *, name: str = "reaper", mode: int = 0o600) -> Path:
    p = home / "gates" / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc))
    p.chmod(mode)
    return p


def test_no_file_is_none(_home: Path) -> None:
    assert tg.load_trusted_gate("reaper") is None


def test_valid_file_loads(_home: Path) -> None:
    _write(_home, _doc())
    g = tg.load_trusted_gate("reaper")
    assert g is not None and g.project_id == "reaper"
    assert g.commands[0].argv == ("./gradlew", ":client:compileJava", "--offline")
    assert g.commands[0].timeout_seconds == 900 and g.commands[0].scope == "unit"
    assert g.env_passthrough == ("PATH", "HOME", "JAVA_HOME")


@pytest.mark.parametrize("mode", [0o666, 0o604, 0o700, 0o664])
def test_insecure_mode_is_refused(_home: Path, mode: int) -> None:
    _write(_home, _doc(), mode=mode)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_mode_insecure"


def test_symlink_is_refused(_home: Path) -> None:
    real = _home / "elsewhere.yaml"
    real.write_text(yaml.safe_dump(_doc()))
    real.chmod(0o600)
    (_home / "gates" / "reaper.yaml").symlink_to(real)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_is_symlink"


def test_wrong_owner_is_refused(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(_home, _doc())
    real_getuid = os.getuid
    monkeypatch.setattr(tg.os, "getuid", lambda: real_getuid() + 1)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_not_owned"


def test_directory_is_refused(_home: Path) -> None:
    p = _home / "gates" / "reaper.yaml"
    p.mkdir()
    p.chmod(0o600)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_not_regular_file"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo not available on this platform")
def test_fifo_is_refused_promptly(_home: Path) -> None:
    p = _home / "gates" / "reaper.yaml"
    os.mkfifo(p)
    p.chmod(0o600)
    start = time.monotonic()
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    elapsed = time.monotonic() - start
    assert ei.value.code == "gate_not_regular_file"
    assert elapsed < 2.0


def test_gates_dir_symlink_is_refused(_home: Path) -> None:
    real_dir = _home / "real_gates"
    real_dir.mkdir()
    p = real_dir / "reaper.yaml"
    p.write_text(yaml.safe_dump(_doc()))
    p.chmod(0o600)
    gates_link = _home / "gates"
    gates_link.rmdir()  # empty dir created by the _home fixture
    gates_link.symlink_to(real_dir)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gates_dir_is_symlink"


def test_unexpected_load_error_is_gate_malformed(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(_home, _doc())

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(tg.yaml, "safe_load", _boom)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_malformed"


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.update(version=2), "bad_version"),
    (lambda d: d.update(created_by="pm"), "created_by_not_operator"),
    (lambda d: d.update(project_id="other"), "project_id_mismatch"),
    (lambda d: d.update(extra=1), "unknown_key"),
    (lambda d: d.update(commands=[]), "no_commands"),
    (lambda d: d.update(commands=[d["commands"][0]] * 9), "too_many_commands"),
    (lambda d: d["commands"][0].update(id="bad/id"), "bad_command_id"),
    (lambda d: d["commands"][0].pop("id"), "bad_command_id"),
    (lambda d: d["commands"][0].update(id=123), "bad_command_id"),
    (lambda d: d["commands"][0].update(id=""), "bad_command_id"),
    (lambda d: d["commands"][0].update(argv="./gradlew build"), "bad_argv"),
    (lambda d: d["commands"][0].update(argv=["./gradlew", "a;b"]), "shell_chars"),
    (lambda d: d["commands"][0].update(argv=["./gradlew", "a\x00b"]), "shell_chars"),
    (lambda d: d["commands"][0].update(argv=["./gradlew", "--no-safety-plane"]), "banned_token"),
    (lambda d: d["commands"][0].update(argv=["gradlew", "build"]), "argv0_not_absolute"),
    (lambda d: d["commands"][0].update(cwd="/abs"), "bad_cwd"),
    (lambda d: d["commands"][0].update(cwd="../x"), "bad_cwd"),
    (lambda d: d["commands"][0].update(cwd="   "), "bad_cwd"),
    (lambda d: d["commands"][0].update(cwd="~/x"), "bad_cwd"),
    (lambda d: d["commands"][0].update(cwd="a\x00b"), "bad_cwd"),
    (lambda d: d["commands"][0].update(timeout_seconds=1801), "bad_timeout"),
    (lambda d: d["commands"][0].update(timeout_seconds=0), "bad_timeout"),
    (lambda d: d["commands"][0].update(timeout_seconds="900"), "bad_timeout"),
    (lambda d: d["commands"][0].update(scope="smoke"), "bad_scope"),
    (lambda d: d["commands"][0].update(bogus=1), "unknown_key"),
    (lambda d: d["env"].update(passthrough=["path"]), "bad_env_name"),
    (lambda d: d["env"].update(passthrough=["AWS_SECRET_ACCESS_KEY"]), "secret_env_name"),
    (lambda d: d["env"].update(passthrough=[f"V{i}" for i in range(33)]), "too_many_env"),
    (lambda d: d["env"].update(other=1), "unknown_key"),
])
def test_each_validation_rule(_home: Path, mutate, code: str) -> None:
    d = _doc()
    mutate(d)
    _write(_home, d)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == code


def test_absolute_argv0_and_mvnw_are_accepted(_home: Path) -> None:
    d = _doc(commands=[
        {"id": "a", "argv": ["/usr/bin/true"], "cwd": "sub", "timeout_seconds": 5},
        {"id": "b", "argv": ["./mvnw", "-q", "test"], "cwd": ".", "timeout_seconds": 5},
    ])
    _write(_home, d)
    g = tg.load_trusted_gate("reaper")
    assert [c.id for c in g.commands] == ["a", "b"] and g.commands[0].scope == "unit"


def test_bad_yaml_is_refused(_home: Path) -> None:
    p = _home / "gates" / "reaper.yaml"
    p.write_text("commands: [\n")
    p.chmod(0o600)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "bad_yaml"


def test_registry_views() -> None:
    g = tg.TrustedGate(
        project_id="reaper", path="/x", env_passthrough=("PATH",),
        commands=(tg.TrustedCommand("compile", ("./gradlew", "build"), ".", 900, "unit"),),
    )
    assert tg.registry_view(g) == {
        "compile": {"argv": ["./gradlew", "build"], "cwd": ".",
                    "timeout_seconds": 900, "scope": "unit", "tier": "trusted"},
    }
    inv = tg.invalid_registry_view("reaper", "gate_mode_insecure")
    assert inv["trusted-gate"]["tier"] == "trusted"
    assert inv["trusted-gate"]["invalid"] == "gate_mode_insecure"
