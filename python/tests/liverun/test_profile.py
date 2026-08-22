# python/tests/liverun/test_profile.py
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from errorta_liverun import profile as P


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _ok_hosts(host: str) -> bool:
    return True


def _minimal(**over) -> dict:
    doc = {
        "version": 1,
        "created_by": "operator",
        "hosts": {"box": {"ssh_host": "box"}},
        "tunnels": {},
        "launch": [
            {"name": "start", "local": {"argv": ["/bin/true"]},
             "check": {"exit0": ["/bin/true"]}, "timeout_s": 5},
        ],
        "watch": [
            {"id": "alive", "every_s": 1, "stall_after_s": 5, "on_stall": "stop",
             "probe": {"http": {"url": "http://127.0.0.1:1/state"}}},
        ],
        "evidence": [],
        "teardown": [
            {"name": "logoff", "check": {"http_json": {"url": "http://127.0.0.1:1/state",
             "path": "gameState", "not_equals": "LOGGED_IN"}}, "timeout_s": 5,
             "evidence_literal": "logoff_verified"},
        ],
        "caps": {},
        "ban_signals": ["Account is banned"],
    }
    doc.update(over)
    return doc


def _write(tmp_path: Path, doc: dict, name: str = "p") -> Path:
    d = P.profiles_dir(); d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.yaml"
    f.write_text(yaml.safe_dump(doc)); f.chmod(0o600)
    return f


def test_minimal_profile_loads(tmp_path: Path) -> None:
    prof = P.load_profile(_write(tmp_path, _minimal()), known_hosts_fn=_ok_hosts)
    assert prof.name == "p"
    assert prof.launch[0].action.kind == "local"
    assert prof.caps == P.DEFAULT_CAPS
    assert prof.teardown[0].evidence_literal == "logoff_verified"


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.update(created_by="slack"), "created_by_not_operator"),
    (lambda d: d.update(version=2), "unsupported_version"),
    (lambda d: d.update(bogus=1), "unknown_key"),
    (lambda d: d["launch"][0].update(local={"argv": ["./jagex-play"]}), "argv0_not_absolute"),
    (lambda d: d["launch"][0].update(local={"argv": ["/bin/sh", "-c", "echo $HOME"]}), "shell_token_in_argv"),
    (lambda d: d["launch"][0].update(local={"argv": ["/bin/true", "--ignore-risk-budget"]}), "banned_token"),
    (lambda d: d.update(caps={"max_launches_per_hour": 3}), "cap_above_default"),
    (lambda d: d.update(teardown=[{"name": "x", "local": {"argv": ["/bin/true"]}, "timeout_s": 1}]), "missing_logoff_literal"),
    (lambda d: d["watch"][0].update(on_stall="explode"), "bad_on_stall"),
    # --- fix round 1: a literal is a claim, and only a CHECK can substantiate one.
    # `remote_signal` exits 0 when there was no pidfile to signal at all, so this
    # step would have forged `logoff_verified` on every run.
    (lambda d: d.update(teardown=[{"name": "logoff", "remote_signal": {
        "host": "box", "pidfile": "~/brain.pid", "signal": "TERM", "then": "KILL"},
        "timeout_s": 1, "evidence_literal": "logoff_verified"}]), "literal_without_check"),
    (lambda d: d.update(teardown=[{"name": "logoff", "local": {"argv": ["/bin/true"]},
        "timeout_s": 1, "evidence_literal": "logoff_verified"}]), "literal_without_check"),
    (lambda d: d["launch"][0].update(remote={"host": "nope", "argv": ["true"]}, local=None), "unknown_host"),
    (lambda d: d["launch"][0].update(remote={"host": "box", "argv": [
        "python", "-m", "senditai_ng.cli", "run", "--execute"]}, local=None), "brain_flags_missing"),
    # --- fix round 1: SSRF-bypassable loopback check ---
    (lambda d: d["watch"][0]["probe"]["http"].update(url="http://127.0.0.1.evil.com/x"), "http_not_loopback"),
    (lambda d: d["watch"][0]["probe"]["http"].update(url="http://127.0.0.1@evil.com/x"), "http_not_loopback"),
    # --- fix round 1: remote_signal fields never validated ---
    (lambda d: d["teardown"].append({"name": "kill", "remote_signal": {
        "host": "box", "pidfile": "~/x.pid", "signal": "TERM; rm -rf /"}, "timeout_s": 1}), "bad_remote_signal"),
    (lambda d: d["teardown"].append({"name": "kill", "remote_signal": {
        "host": "box", "pidfile": "~/x.pid; rm -rf /", "signal": "TERM"}, "timeout_s": 1}), "bad_path"),
    # --- fix round 1: same path regex applies to remote.pidfile / remote.log / remote_file_mtime_advancing.path ---
    (lambda d: d["launch"][0].update(remote={"host": "box", "argv": ["/bin/true"],
        "detach": True, "pidfile": "~/x.pid; rm -rf /"}, local=None), "bad_path"),
    (lambda d: d["launch"][0].update(remote={"host": "box", "argv": ["/bin/true"],
        "log": "~/x.log; rm -rf /"}, local=None), "bad_path"),
    (lambda d: d["watch"][0].update(probe={"remote_file_mtime_advancing": {
        "host": "box", "path": "~/x.log; rm -rf /"}}), "bad_path"),
    # --- fix round 1: banned-token exact-match-only bypass via "=value" ---
    (lambda d: d["launch"][0].update(local={"argv": ["/bin/true", "--ignore-risk-budget=1"]}), "banned_token"),
    # --- fix round 1: uniform numeric guarding ---
    (lambda d: d["launch"][0].update(timeout_s="abc"), "bad_number"),
    (lambda d: d.update(caps={"max_launches_per_hour": "two"}), "bad_number"),
    # --- fix round 2: fail-closed unknown check-param keys, per kind ---
    (lambda d: d["launch"][0].update(check={"http": {"url": "http://127.0.0.1:1/x",
        "expect_status": 200, "bogus": 1}}), "unknown_key"),
    (lambda d: d["teardown"][0]["check"]["http_json"].update(bogus=1), "unknown_key"),
    (lambda d: d["launch"][0].update(check={"remote_pid_alive": {
        "host": "box", "pidfile": "~/x.pid", "bogus": 1}}), "unknown_key"),
    (lambda d: d["launch"][0].update(check={"file_mtime_newer": {
        "path": "/tmp/x", "bogus": 1}}), "unknown_key"),
    (lambda d: d["launch"][0].update(check={"file_mtime_newer": {
        "path": "/tmp/x", "than": "not_step_start"}}), "bad_check"),
    # --- fix round 2: fail-closed required fields on remote_* probes ---
    (lambda d: d["watch"][0].update(probe={"remote_stdout_advancing": {"host": "box"}}), "bad_probe"),
    (lambda d: d["watch"][0].update(probe={"remote_stdout_matches": {
        "host": "box", "argv": ["/bin/true"]}}), "bad_probe"),
    (lambda d: d["watch"][0].update(probe={"remote_stdout_matches": {
        "host": "box", "argv": ["/bin/true"], "regex": "("}}), "bad_probe"),
    (lambda d: d["watch"][0].update(probe={"remote_file_mtime_advancing": {"host": "box"}}), "bad_probe"),
])
def test_validator_rejects(tmp_path: Path, mutate, code: str) -> None:
    doc = _minimal(); mutate(doc)
    if doc["launch"][0].get("local") is None:
        doc["launch"][0].pop("local", None)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)
    assert ei.value.code == code


def test_gradlew_needs_absolute_cwd(tmp_path: Path) -> None:
    doc = _minimal()
    doc["launch"][0]["local"] = {"argv": ["./gradlew", "build"]}
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)
    assert ei.value.code == "argv0_not_absolute"
    doc["launch"][0]["local"] = {"argv": ["./gradlew", "build"], "cwd": "/abs/repo"}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_brain_run_with_required_flags_passes(tmp_path: Path) -> None:
    doc = _minimal()
    doc["launch"][0] = {"name": "brain", "remote": {"host": "box", "argv": [
        "python", "-m", "senditai_ng.cli", "run", "--max-session-seconds", "3600",
        "--receipt-id", "r", "--require-live-feed"], "detach": True,
        "pidfile": "~/x.pid"}, "check": {"remote_pid_alive": {"host": "box", "pidfile": "~/x.pid"}},
        "timeout_s": 5}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_brain_run_accepts_equals_form_flags(tmp_path: Path) -> None:
    doc = _minimal()
    doc["launch"][0] = {"name": "brain", "remote": {"host": "box", "argv": [
        "python", "-m", "senditai_ng.cli", "run", "--max-session-seconds=3600",
        "--receipt-id=r", "--require-live-feed"], "detach": True,
        "pidfile": "~/x.pid"}, "check": {"remote_pid_alive": {"host": "box", "pidfile": "~/x.pid"}},
        "timeout_s": 5}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_unknown_known_hosts_rejected(tmp_path: Path) -> None:
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(_write(tmp_path, _minimal()), known_hosts_fn=lambda h: False)
    assert ei.value.code == "host_unknown"


def test_symlink_and_wrong_mode_rejected(tmp_path: Path) -> None:
    f = _write(tmp_path, _minimal())
    f.chmod(0o666)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(f, known_hosts_fn=_ok_hosts)
    assert ei.value.code == "profile_mode_insecure"
    f.chmod(0o600)
    link = f.parent / "link.yaml"; link.symlink_to(f)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(link, known_hosts_fn=_ok_hosts)
    assert ei.value.code == "profile_is_symlink"


def test_outside_profiles_dir_rejected(tmp_path: Path) -> None:
    f = tmp_path / "elsewhere.yaml"; f.write_text(yaml.safe_dump(_minimal())); f.chmod(0o600)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(f, known_hosts_fn=_ok_hosts)
    assert ei.value.code == "profile_outside_dir"


def test_list_profiles_reports_validity(tmp_path: Path) -> None:
    _write(tmp_path, _minimal(), "good")
    _write(tmp_path, _minimal(created_by="slack"), "bad")
    rows = {r["name"]: r for r in P.list_profiles(known_hosts_fn=_ok_hosts)}
    assert rows["good"]["valid"] is True
    assert rows["bad"]["valid"] is False and rows["bad"]["error"] == "created_by_not_operator"


# --- fix round 1 coverage -------------------------------------------------- #

def test_loopback_url_with_port_accepted(tmp_path: Path) -> None:
    doc = _minimal()
    doc["watch"][0]["probe"]["http"]["url"] = "http://127.0.0.1:8081/state"
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_remote_signal_accepts_valid_fields(tmp_path: Path) -> None:
    doc = _minimal()
    doc["teardown"].append({
        "name": "kill",
        "remote_signal": {"host": "box", "pidfile": "~/x.pid", "signal": "TERM",
                           "then": "KILL", "grace_s": 5},
        "timeout_s": 1,
    })
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


# --- fix round 2 coverage --------------------------------------------------- #

def test_remote_stdout_advancing_with_argv_passes(tmp_path: Path) -> None:
    doc = _minimal()
    doc["watch"][0]["probe"] = {"remote_stdout_advancing": {"host": "box",
        "argv": ["tail", "-n", "1", "/tmp/x.log"]}}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_remote_stdout_matches_with_argv_and_regex_passes(tmp_path: Path) -> None:
    doc = _minimal()
    doc["watch"][0]["probe"] = {"remote_stdout_matches": {"host": "box",
        "argv": ["tail", "-n", "1", "/tmp/x.log"], "regex": r'"live":\s*true'}}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_remote_file_mtime_advancing_with_path_passes(tmp_path: Path) -> None:
    doc = _minimal()
    doc["watch"][0]["probe"] = {"remote_file_mtime_advancing": {"host": "box", "path": "/tmp/x.log"}}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_file_mtime_newer_check_accepts_than_step_start(tmp_path: Path) -> None:
    doc = _minimal()
    doc["launch"][0]["check"] = {"file_mtime_newer": {"path": "/tmp/x", "than": "step_start"}}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_list_profiles_survives_malformed_yaml(tmp_path: Path) -> None:
    _write(tmp_path, _minimal(), "good")
    bad = _minimal()
    bad["hosts"] = ["not", "a", "dict"]
    _write(tmp_path, bad, "bad")
    rows = {r["name"]: r for r in P.list_profiles(known_hosts_fn=_ok_hosts)}
    assert rows["good"]["valid"] is True
    assert rows["bad"]["valid"] is False
    assert rows["bad"]["error"] in ("bad_host", "profile_malformed")


# --- Slice 2: repos / fix_loop --------------------------------------------- #

@pytest.fixture
def valid_doc() -> dict:
    return _minimal()


def _load(tmp_path: Path, doc: dict, *, name: str = "p",
          known_hosts_fn=_ok_hosts, **kw):
    return P.load_profile(_write(tmp_path, doc, name), known_hosts_fn=known_hosts_fn, **kw)


def _repo_dir(tmp_path: Path, name: str = "senditai-ng") -> Path:
    d = tmp_path / name
    (d / ".git").mkdir(parents=True, exist_ok=True)
    return d


def _repos_doc(**over) -> dict:
    # NOTE: no `check:` on the deploy step. The design sketch wrote
    # `check: {exit0: true}`, but Slice 1's `exit0` check IS an argv to run
    # (`profile._check` -> `_argv`, `steps.run_check` -> `_spawn_tracked`), and
    # deploy steps are validated by the *existing* `_step()` unchanged. A bare
    # `true` would need a new check form with no runtime behind it -- exactly the
    # silently-ignored field `CHECK_ALLOWED_KEYS` exists to prevent.
    repo = {"id": "brain", "path": None, "errorta_project": "senditai-ng",
            "classify": ["python_traceback", "brain_log_stall"],
            "deploy": [{"name": "rsync", "local": {"argv": [
                "/usr/bin/rsync", "-az", "--delete", "--exclude", ".git",
                "/Users/OPERATOR/GitHub/senditai-ng/", "senditai:senditai-ng/"]},
                "timeout_s": 300}]}
    repo.update(over)
    return repo


def test_repos_and_fix_loop_load(tmp_path: Path, valid_doc: dict) -> None:
    repo_dir = _repo_dir(tmp_path)
    doc = dict(valid_doc)
    doc["repos"] = [_repos_doc(path=str(repo_dir))]
    doc["fix_loop"] = {"enabled": True, "max_fix_cycles_per_day": 3,
                       "idle_timeout_s": 1200, "triage_route": "pm"}
    prof = _load(tmp_path, doc, project_exists_fn=lambda pid: True)
    assert prof.fix_loop.enabled and prof.fix_loop.max_fix_cycles_per_day == 3
    assert prof.fix_loop.accept_timeout_s == 1800
    repo = prof.repo_by_id("brain")
    assert repo.fixable is True and repo.deploy[0].action.kind == "local"
    # the rsync argv survives _argv unchanged -- the trailing slash is load-bearing
    assert repo.deploy[0].action.params["argv"][-2].endswith("senditai-ng/")
    assert prof.repo_by_id("nope") is None


def test_profile_without_repos_is_still_valid(tmp_path: Path, valid_doc: dict) -> None:
    prof = _load(tmp_path, dict(valid_doc))
    assert prof.repos == () and prof.fix_loop is None


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d["repos"][0].update(path="senditai-ng"), "repo_path_not_absolute"),
    (lambda d: d["repos"][0].update(errorta_project="nope"), "unknown_errorta_project"),
    (lambda d: d["repos"][0].update(classify=["not_a_class"]), "unknown_evidence_class"),
    (lambda d: d["repos"][0].update(classify=["launch_step_failed:nope"]), "unknown_launch_step"),
    (lambda d: d["repos"][0].update(classify=["launch_step_failed:"]), "unknown_launch_step"),
    (lambda d: d["repos"][0]["deploy"][0]["local"].update(argv=["rsync", "-a"]),
     "argv0_not_absolute"),
    (lambda d: d["repos"].append(_repos_doc(id="brain")), "duplicate_repo_id"),
    (lambda d: d["repos"][0].update(id="Brain"), "bad_repo_id"),
    (lambda d: d["repos"][0].update(bogus=1), "unknown_key"),
    (lambda d: d["repos"][0].update(fixable="yes"), "bad_repo"),
    (lambda d: d["repos"][0]["deploy"][0].update(
        local=None, window_shot={"pgrep": "x"}), "bad_deploy_step"),
    (lambda d: d["fix_loop"].update(max_fix_cycles_per_day=9), "cap_raised"),
    (lambda d: d["fix_loop"].update(idle_timeout_s=300), "idle_below_turn_timeout"),
    (lambda d: d["fix_loop"].update(idle_timeout_s=9000), "cap_raised"),
    (lambda d: d["fix_loop"].update(accept_timeout_s=9999), "cap_raised"),
    (lambda d: d["fix_loop"].update(triage_route="claude_cli.opus"), "bad_triage_route"),
    (lambda d: d["fix_loop"].update(bogus=1), "unknown_key"),
    (lambda d: d.pop("repos"), "fix_loop_without_repos"),
    (lambda d: d["repos"][0].update(fixable=False), "fix_loop_without_repos"),
])
def test_repo_validator_rejects(tmp_path: Path, valid_doc: dict, mutate, code: str) -> None:
    repo_dir = _repo_dir(tmp_path)
    doc = dict(valid_doc)
    doc["repos"] = [_repos_doc(path=str(repo_dir))]
    doc["fix_loop"] = {"enabled": True}
    mutate(doc)
    for r in doc.get("repos") or []:
        for s in r.get("deploy") or []:
            if "local" in s and s["local"] is None:
                s.pop("local")
    with pytest.raises(P.ProfileError) as exc:
        _load(tmp_path, doc, project_exists_fn=lambda pid: pid == "senditai-ng")
    assert exc.value.code == code


def test_repo_path_must_exist_and_be_a_checkout(tmp_path: Path, valid_doc: dict) -> None:
    doc = dict(valid_doc)
    doc["repos"] = [_repos_doc(path=str(tmp_path / "ghost"))]
    with pytest.raises(P.ProfileError) as exc:
        _load(tmp_path, doc, project_exists_fn=lambda pid: True)
    assert exc.value.code == "repo_path_missing"
    plain = tmp_path / "plain"; plain.mkdir()
    doc["repos"] = [_repos_doc(path=str(plain))]
    with pytest.raises(P.ProfileError) as exc:
        _load(tmp_path, doc, project_exists_fn=lambda pid: True)
    assert exc.value.code == "repo_path_missing"


def test_two_repos_may_not_claim_the_same_class(tmp_path: Path, valid_doc: dict) -> None:
    doc = dict(valid_doc)
    doc["repos"] = [
        _repos_doc(path=str(_repo_dir(tmp_path))),
        _repos_doc(id="reaper", path=str(_repo_dir(tmp_path, "osrs-reaper")),
                   errorta_project="osrs-reaper", deploy=[],
                   classify=["jvm_exception", "brain_log_stall"]),
    ]
    with pytest.raises(P.ProfileError) as exc:
        _load(tmp_path, doc, project_exists_fn=lambda pid: True)
    assert exc.value.code == "ambiguous_class_mapping"


def test_unfixable_repo_is_allowed_when_another_repo_is_fixable(
        tmp_path: Path, valid_doc: dict) -> None:
    doc = dict(valid_doc)
    doc["repos"] = [
        _repos_doc(path=str(_repo_dir(tmp_path))),
        _repos_doc(id="reaper", path=str(_repo_dir(tmp_path, "osrs-reaper")),
                   errorta_project="osrs-reaper", fixable=False, deploy=[],
                   classify=["jvm_exception"]),
    ]
    doc["fix_loop"] = {"enabled": True}
    prof = _load(tmp_path, doc, project_exists_fn=lambda pid: True)
    assert prof.repo_by_id("reaper").fixable is False
    assert prof.repo_by_id("reaper").deploy == ()


def test_default_project_exists_is_false_without_a_ledger(monkeypatch) -> None:
    # the seam's production default must fail CLOSED, never raise
    monkeypatch.setattr(P, "_import_ledger_store", lambda: None)
    assert P.default_project_exists("anything") is False


# --------------------------------------------------------------------------
# The SHIPPED example profile. `docs/liverun/example-profile.yaml` is invalid
# by construction as a whole (every operator-specific value is a `# FILL:`
# line, including empty argvs the loader rejects) -- so what is asserted here
# is the part the operator does NOT have to author: the `repos:` / `fix_loop:`
# block ships complete except for the two checkout paths, and it must load.
#
# Without this, the shipped example is only as correct as the last person to
# hand-edit it -- and an example that cannot load is worse than none, because
# the operator debugs their own typing against a broken skeleton.
# --------------------------------------------------------------------------

DOCS = Path(__file__).resolve().parents[3] / "docs" / "liverun"


def _example_doc(tmp_path: Path, valid_doc: dict) -> dict:
    """The shipped `repos:`/`fix_loop:` block on a loadable profile.

    The example's own `launch:` cannot be loaded (its argvs are `# FILL:`
    lines), but its step NAMES are shipped, and `classify:` entries of the form
    `launch_step_failed:<name>` are resolved against them at load. So the step
    names come from the example and only the argvs are stubbed -- otherwise
    this would assert the block against launch steps the operator will never
    have.
    """
    example = yaml.safe_load((DOCS / "example-profile.yaml").read_text())
    doc = dict(valid_doc)
    doc["launch"] = [{"name": step["name"], "local": {"argv": ["/bin/true"]},
                      "check": {"exit0": ["/bin/true"]}, "timeout_s": 5}
                     for step in example["launch"]]
    doc["repos"] = example["repos"]
    doc["fix_loop"] = example["fix_loop"]
    # The one substitution: `/Users/OPERATOR/...` is the shape of a path, not a
    # path. Every other value in the block is shipped as-is.
    for i, name in enumerate(("senditai-ng", "osrs-reaper")):
        doc["repos"][i]["path"] = str(_repo_dir(tmp_path, name))
    return doc


def test_the_shipped_example_fix_loop_block_validates(tmp_path: Path,
                                                      valid_doc: dict) -> None:
    doc = _example_doc(tmp_path, valid_doc)

    prof = _load(tmp_path, doc, project_exists_fn=lambda pid: True)

    assert [r.id for r in prof.repos] == ["brain", "reaper"]
    assert prof.fix_loop.enabled
    assert prof.repo_by_id("brain").deploy[0].action.kind == "local"
    # `reaper` has no registrable gate (G-3), and the profile's `rebuild-jar`
    # launch step already redeploys it.
    assert prof.repo_by_id("reaper").fixable is False
    assert prof.repo_by_id("reaper").deploy == ()


@pytest.mark.parametrize("reason,repo_id", [
    ("launch_step_failed:brain", "brain"),
    ("launch_step_failed:rebuild-jar", "reaper"),
])
def test_the_shipped_example_routes_a_failed_launch_step(
        tmp_path: Path, valid_doc: dict, reason: str, repo_id: str) -> None:
    """A launch step that fails carries the generic `launch_step_failed` class,
    which no repo may claim without claiming every launch failure -- so an
    example that named no steps would pause for a human on the two most likely
    failures it has. Whoever wrote the step owns it, and the example says so."""
    from errorta_liverun import triage
    from errorta_liverun.brief import EvidenceBundle

    prof = _load(tmp_path, _example_doc(tmp_path, valid_doc),
                 project_exists_fn=lambda pid: True)
    bundle = EvidenceBundle(profile_name="osrs", run_id="r-1", stop_reason=reason,
                            launch_step_name=reason.split(":", 1)[1])

    res = triage.classify(bundle, prof)

    assert res.repo_id == repo_id
    assert res.confidence == triage.CONFIDENCE_DETERMINISTIC


def test_the_shipped_example_declares_no_check_on_a_deploy_step() -> None:
    """The design sketch wrote `check: {exit0: true}`, which does not validate:
    Slice 1's `exit0` IS an argv to run. An example carrying it would fail the
    load with `argv_not_list_of_str` on the operator's first attempt."""
    example = yaml.safe_load((DOCS / "example-profile.yaml").read_text())

    for repo in example["repos"]:
        for step in repo.get("deploy") or []:
            assert "check" not in step or isinstance(
                (step["check"] or {}).get("exit0"), list), step


def test_a_repo_may_claim_one_named_launch_step(tmp_path: Path, valid_doc: dict) -> None:
    """`launch_step_failed` on its own is a class every repo would have to fight
    over. A launch step belongs to whoever wrote it, so the operator says so."""
    repo_dir = _repo_dir(tmp_path)
    doc = dict(valid_doc)
    doc["repos"] = [_repos_doc(path=str(repo_dir),
                               classify=["brain_log_stall", "launch_step_failed:start"])]
    doc["fix_loop"] = {"enabled": True}
    prof = _load(tmp_path, doc, project_exists_fn=lambda pid: True)
    assert "launch_step_failed:start" in prof.repo_by_id("brain").classify


def test_two_repos_may_not_claim_the_same_launch_step(tmp_path: Path, valid_doc: dict) -> None:
    repo_dir = _repo_dir(tmp_path)
    other = _repo_dir(tmp_path, "osrs-reaper")
    doc = dict(valid_doc)
    doc["repos"] = [_repos_doc(path=str(repo_dir), classify=["launch_step_failed:start"]),
                    _repos_doc(id="reaper", path=str(other), deploy=[],
                               classify=["launch_step_failed:start"])]
    doc["fix_loop"] = {"enabled": True}
    with pytest.raises(P.ProfileError) as exc:
        _load(tmp_path, doc, project_exists_fn=lambda pid: True)
    assert exc.value.code == "ambiguous_class_mapping"
