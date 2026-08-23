from __future__ import annotations

import hashlib
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


def test_passthrough_env_copies_only_listed_non_secret_names(monkeypatch) -> None:
    monkeypatch.setenv("JAVA_HOME", "/jdk")
    monkeypatch.setenv("MY_API_KEY", "s3cret")
    env = tg.passthrough_env(("JAVA_HOME", "MY_API_KEY", "NOT_SET_ANYWHERE_X"))
    assert env == {"JAVA_HOME": "/jdk"}


def test_run_trusted_command_passes_and_records_trusted(tmp_path: Path) -> None:
    res = tg.run_trusted_command(
        {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
        command_id="ok", workspace_root=tmp_path, env_passthrough=("PATH",))
    assert res.passed and res.status == "completed" and res.exit_code == 0
    assert res.command_id == "ok" and len(res.argv_sha256) == 64


def test_run_trusted_command_failure_is_a_real_red(tmp_path: Path) -> None:
    res = tg.run_trusted_command(
        {"argv": ["/usr/bin/false"], "cwd": ".", "timeout_seconds": 5},
        command_id="no", workspace_root=tmp_path, env_passthrough=())
    assert not res.passed and res.status == "failed"
    assert res.exit_code == 1 and res.reason == "exit 1"


def test_run_trusted_command_times_out_and_kills_the_group(tmp_path: Path) -> None:
    import time
    t0 = time.monotonic()
    res = tg.run_trusted_command(
        {"argv": ["/bin/sleep", "30"], "cwd": ".", "timeout_seconds": 1},
        command_id="slow", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "timed_out" and not res.passed and "timed out" in res.reason
    assert time.monotonic() - t0 < 10


def test_run_trusted_command_cwd_is_inside_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    res = tg.run_trusted_command(
        {"argv": ["/bin/pwd"], "cwd": "sub", "timeout_seconds": 5},
        command_id="pwd", workspace_root=tmp_path, env_passthrough=())
    assert res.passed and str((tmp_path / "sub").resolve()) in res.stdout_preview


def test_run_trusted_command_invalid_marker_is_blocked(tmp_path: Path) -> None:
    spec = tg.invalid_registry_view("reaper", "gate_mode_insecure")["trusted-gate"]
    res = tg.run_trusted_command(
        spec, command_id="trusted-gate", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "blocked" and not res.passed
    assert res.reason == "trusted_gate_invalid:gate_mode_insecure"


def test_run_trusted_command_cancel_before_launch(tmp_path: Path) -> None:
    res = tg.run_trusted_command(
        {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
        command_id="c", workspace_root=tmp_path, env_passthrough=(),
        should_cancel=lambda: True)
    assert res.status == "blocked" and res.reason == "cancelled before launch"


def test_load_trusted_gate_stat_race_is_gate_malformed(
        _home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(_home, _doc())
    real_stat = Path.stat

    def _boom(self, *a, **kw):
        if self.name == "reaper.yaml":
            raise PermissionError("race")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _boom)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_malformed"


def test_run_trusted_command_killpg_eperm_is_tolerated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS raises PermissionError (EPERM), not ProcessLookupError, from
    # os.killpg when the group's only member is a just-exited zombie. Both
    # killpg calls (TERM and KILL) must tolerate this without an exception
    # escaping, still landing a timed_out record.
    monkeypatch.setattr(os, "killpg", lambda pid, sig: (_ for _ in ()).throw(PermissionError))
    res = tg.run_trusted_command(
        {"argv": ["/bin/sleep", "2"], "cwd": ".", "timeout_seconds": 1},
        command_id="eperm", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "timed_out" and not res.passed


def test_run_trusted_command_abandons_output_from_a_detached_grandchild(
        tmp_path: Path) -> None:
    # A grandchild that calls os.setsid() moves to a new session/process
    # group and so survives our killpg on the direct child's group, but it
    # still holds the inherited stdout/stderr pipe fds open — the pipe never
    # sees EOF. macOS ships no standalone `setsid` binary, so the detach is
    # done with a python3 fork()+os.setsid(), per the review's own fallback.
    script = ("python3 -c 'import os,time,sys; pid=os.fork(); "
              "(os.setsid(), time.sleep(20)) if pid==0 else None' & sleep 30")
    t0 = time.monotonic()
    res = tg.run_trusted_command(
        {"argv": ["/bin/sh", "-c", script], "cwd": ".", "timeout_seconds": 1},
        command_id="grandchild", workspace_root=tmp_path, env_passthrough=("PATH",))
    assert res.status == "timed_out" and not res.passed
    assert "output abandoned" in res.reason
    assert time.monotonic() - t0 < 12


def test_run_trusted_command_caps_output_like_the_sandboxed_tier(tmp_path: Path) -> None:
    script = "head -c 3000000 /dev/zero | tr '\\0' a"
    res = tg.run_trusted_command(
        {"argv": ["/bin/sh", "-c", script], "cwd": ".", "timeout_seconds": 10},
        command_id="big", workspace_root=tmp_path, env_passthrough=())
    assert res.passed and res.status == "completed"
    expected = hashlib.sha256(b"a" * tg.MAX_OUTPUT_BYTES).hexdigest()
    assert res.stdout_sha256 == expected


def test_run_trusted_command_cwd_symlink_escaping_workspace_is_blocked(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-workspace"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside)
    res = tg.run_trusted_command(
        {"argv": ["/bin/pwd"], "cwd": "escape", "timeout_seconds": 5},
        command_id="escape", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "blocked" and res.reason == "cwd_outside_workspace"


def test_run_trusted_command_missing_binary_is_a_labeled_failure(tmp_path: Path) -> None:
    res = tg.run_trusted_command(
        {"argv": ["/nonexistent/bin"], "cwd": ".", "timeout_seconds": 5},
        command_id="nf", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "failed" and not res.passed and res.exit_code is None
    assert res.reason == "launch failed: FileNotFoundError"


def test_run_trusted_command_passthrough_value_reaches_the_child(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "reaches-the-child")
    res = tg.run_trusted_command(
        {"argv": ["/usr/bin/env"], "cwd": ".", "timeout_seconds": 5},
        command_id="envtest", workspace_root=tmp_path, env_passthrough=("MY_VAR",))
    assert res.passed and "MY_VAR=reaches-the-child" in res.stdout_preview
