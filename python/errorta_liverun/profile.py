"""Profile schema + fail-closed validator (spec §3.2).

A `remote` action with `detach: true` requires a long-lived command: the
launcher (`errorta_liverun.steps`) checks liveness ~0.2s after spawn, so
anything that exits within that window is reported as a failed launch.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from errorta_app.paths import errorta_home

LOCAL_ARGV0_ALLOWLIST = ("osascript", "pgrep")
BANNED_TOKENS = ("--ignore-risk-budget", "--no-safety-plane")
BRAIN_REQUIRED_FLAGS = ("--max-session-seconds", "--receipt-id", "--require-live-feed")
ALLOWED_TOKENS = ("$SESSION_ID", "$RUN_ID")
LOOPBACK_HOSTNAMES = ("127.0.0.1", "localhost", "::1")
REMOTE_SIGNALS = ("TERM", "KILL", "INT", "HUP")
_SHELL_CHARS = re.compile(r"[$`|;&<>]")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PATH_RE = re.compile(r"^[A-Za-z0-9~/._-]+$")

CHECK_KINDS = {"exit0", "http", "http_json", "file_exists", "file_mtime_newer", "pgrep",
               "pgrep_absent", "tunnel_up", "remote_pid_alive", "all"}
# Dict-shaped check kinds: exactly these params, nothing else. Fail closed —
# an unrecognised key is far more likely a typo'd or half-migrated field the
# runtime silently ignores (see `than`, below) than an intentional no-op.
CHECK_ALLOWED_KEYS = {
    "http": {"url", "expect_status"},
    "http_json": {"url", "path", "equals", "not_equals"},
    "file_mtime_newer": {"path", "than"},
    "remote_pid_alive": {"host", "pidfile"},
}
PROBE_KINDS = {"http", "remote_pid_alive", "remote_file_mtime_advancing",
               "remote_stdout_advancing", "remote_stdout_matches", "elapsed_lt_s"}
ACTION_KINDS = {"local", "remote", "remote_signal", "tunnel", "tunnel_close", "window_shot", "http"}
TOP_KEYS = {"version", "created_by", "hosts", "tunnels", "launch", "watch", "evidence",
            "teardown", "caps", "ban_signals", "repos", "fix_loop"}
STEP_KEYS = {"name", "check", "timeout_s", "evidence_literal"} | ACTION_KINDS

# --- Slice 2: the fix loop ------------------------------------------------- #
# The closed evidence vocabulary a profile may map onto a repo. Closed by
# design: a class name is what the deterministic triage signatures in
# `errorta_liverun.triage` implement, so an unrecognised one in a profile is a
# rule that would never fire -- silently, forever. Fail closed at load.
EVIDENCE_CLASSES = frozenset({
    "python_traceback", "brain_log_stall", "journal_stall", "brain_pid_dead",
    "jvm_exception", "client_port_dead", "client_state_stale", "launch_step_failed",
})
TRIAGE_ROUTES = ("pm",)
# A `classify:` entry may also name ONE launch step: `launch_step_failed:<name>`.
# Which repo owns a failed launch step is a fact the operator already knows (the
# step is theirs), so it is declared, not inferred -- without it a launch-step
# failure has only the generic `launch_step_failed` class, which every repo would
# have to fight over.
LAUNCH_STEP_CLASS_PREFIX = "launch_step_failed:"
# A deploy step may act locally, remotely, or signal a remote pid. `tunnel` /
# `tunnel_close` / `window_shot` / `http` are launch-time concerns and are
# rejected in a deploy list.
DEPLOY_ACTION_KINDS = {"local", "remote", "remote_signal"}
FIX_CAP_DEFAULTS = {"max_fix_cycles_per_day": 3, "idle_timeout_s": 2400,
                    "accept_timeout_s": 1800}
# One dev turn can be TWO subprocess attempts back to back: the retrieval
# attempt at ERRORTA_REPO_READ_TIMEOUT_S (async_claude_cli.py, default 1500 s),
# and — when that returns empty — the plain fallback at the request's own
# timeout (errorta_council/reasoning_budget.py default_timeout_seconds, 600 s).
# The idle detector must outlast both attempts combined (1500 + 600), or a
# turn that falls through to the fallback gets killed as "idle" mid-turn.
MIN_IDLE_TIMEOUT_S = 2100
REPO_KEYS = {"id", "path", "errorta_project", "fixable", "classify", "deploy"}
FIX_LOOP_KEYS = {"enabled", "triage_route", "dev_route"} | set(FIX_CAP_DEFAULTS)
_REPO_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DEV_ROUTE_RE = re.compile(r"^[a-z_]+\.[a-z0-9][a-z0-9_.-]*")


class ProfileError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Host:
    ssh_host: str
    ssh_port: int | None = None
    ssh_username: str | None = None
    ssh_key_path: str | None = None


@dataclass(frozen=True)
class TunnelDef:
    id: str
    host: str
    reverse: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Check:
    kind: str
    params: Any


@dataclass(frozen=True)
class Probe:
    kind: str
    params: Any


@dataclass(frozen=True)
class Action:
    kind: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Step:
    name: str
    action: Action | None
    check: Check | None
    timeout_s: float
    evidence_literal: str | None = None


@dataclass(frozen=True)
class WatchProbe:
    id: str
    every_s: float
    stall_after_s: float
    on_stall: str
    probe: Probe


@dataclass(frozen=True)
class Caps:
    max_launches_per_hour: int = 2
    min_launch_gap_s: int = 900
    max_launches_per_day: int = 8
    max_consecutive_failed_cycles: int = 2


DEFAULT_CAPS = Caps()


@dataclass(frozen=True)
class RepoDef:
    id: str
    path: str
    errorta_project: str
    fixable: bool = True
    classify: tuple[str, ...] = ()
    deploy: tuple[Step, ...] = ()


@dataclass(frozen=True)
class FixLoop:
    enabled: bool = False
    max_fix_cycles_per_day: int = 3
    idle_timeout_s: int = 2400
    triage_route: str = "pm"
    dev_route: str = "claude_cli.opus"
    accept_timeout_s: int = 1800


@dataclass(frozen=True)
class Profile:
    name: str
    hosts: dict[str, Host]
    tunnels: dict[str, TunnelDef]
    launch: tuple[Step, ...]
    watch: tuple[WatchProbe, ...]
    evidence: tuple[Step, ...]
    teardown: tuple[Step, ...]
    caps: Caps = DEFAULT_CAPS
    ban_signals: tuple[str, ...] = ()
    repos: tuple[RepoDef, ...] = ()
    fix_loop: FixLoop | None = None

    def repo_by_id(self, rid: str) -> RepoDef | None:
        for r in self.repos:
            if r.id == rid:
                return r
        return None


def profiles_dir() -> Path:
    return errorta_home() / "liverun" / "profiles"


def _import_ledger_store():
    """Seam: the council package is an optional dependency of this module."""
    try:
        from errorta_council.coding.ledger import LedgerStore
    except Exception:  # noqa: BLE001 - a missing/renamed package must not raise here
        return None
    return LedgerStore


def default_project_exists(project_id: str) -> bool:
    """Does `project_id` resolve in the coding ledger? Lazy, so profile.py still
    imports without the council package, and fail-closed: any error at all --
    ProjectNotFound, a broken store, an import failure -- means "no"."""
    store_cls = _import_ledger_store()
    if store_cls is None:
        return False
    try:
        store_cls(project_id).get_project()
    except Exception:  # noqa: BLE001 - ProjectNotFound and any store error alike
        return False
    return True


def default_known_hosts_check(host: str) -> bool:
    try:
        rc = subprocess.run(["ssh-keygen", "-F", host], capture_output=True, timeout=5).returncode
    except (OSError, subprocess.TimeoutExpired):
        return False
    return rc == 0


# --- validation helpers -------------------------------------------------- #

def _num(value: Any, *, where: str, code: str = "bad_number") -> float:
    """Reject anything that isn't a real number — notably bools (True/False
    are `int` subclasses in Python and would otherwise silently coerce)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(code, where)
    return float(value)


def _path(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not _PATH_RE.match(value):
        raise ProfileError("bad_path", where)
    return value


def _loopback_url(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise ProfileError("http_not_loopback", where)
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "http":
        raise ProfileError("http_not_loopback", where)
    if parts.username is not None or parts.password is not None:
        raise ProfileError("http_not_loopback", where)
    if parts.hostname not in LOOPBACK_HOSTNAMES:
        raise ProfileError("http_not_loopback", where)
    return value


def _flag_present(flag: str, argv: tuple[str, ...]) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def _argv(value: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(a, str) for a in value):
        raise ProfileError("argv_not_list_of_str", where)
    for a in value:
        for banned in BANNED_TOKENS:
            if a == banned or a.startswith(banned + "="):
                raise ProfileError("banned_token", f"{where}: {a}")
        stripped = a
        for tok in ALLOWED_TOKENS:
            stripped = stripped.replace(tok, "")
        if _SHELL_CHARS.search(stripped):
            raise ProfileError("shell_token_in_argv", f"{where}: {a!r}")
    return tuple(value)


def _local_action(raw: dict[str, Any], *, where: str) -> Action:
    argv = _argv(raw.get("argv"), where=where)
    cwd = raw.get("cwd")
    a0 = argv[0]
    if a0 == "./gradlew":
        if not (isinstance(cwd, str) and os.path.isabs(cwd)):
            raise ProfileError("argv0_not_absolute", f"{where}: ./gradlew needs absolute cwd")
    elif not (os.path.isabs(a0) or a0 in LOCAL_ARGV0_ALLOWLIST):
        raise ProfileError("argv0_not_absolute", f"{where}: {a0!r}")
    if cwd is not None and not (isinstance(cwd, str) and os.path.isabs(cwd)):
        raise ProfileError("cwd_not_absolute", where)
    return Action("local", {"argv": argv, "cwd": cwd})


def _remote_action(raw: dict[str, Any], hosts: dict[str, Host], *, where: str) -> Action:
    host = raw.get("host")
    if host not in hosts:
        raise ProfileError("unknown_host", f"{where}: {host!r}")
    argv = _argv(raw.get("argv"), where=where)
    if "senditai_ng.cli" in argv and "run" in argv:
        missing = [f for f in BRAIN_REQUIRED_FLAGS if not _flag_present(f, argv)]
        if missing:
            raise ProfileError("brain_flags_missing", f"{where}: {missing}")
    detach = bool(raw.get("detach", False))
    pidfile = raw.get("pidfile")
    if pidfile is not None:
        pidfile = _path(pidfile, where=where)
    if detach and pidfile is None:
        raise ProfileError("detach_needs_pidfile", where)
    stdin_file = raw.get("stdin_file")
    if stdin_file is not None and not (isinstance(stdin_file, str) and os.path.isabs(os.path.expanduser(stdin_file))):
        raise ProfileError("stdin_file_not_absolute", where)
    log = raw.get("log")
    if log is not None:
        log = _path(log, where=where)
    return Action("remote", {"host": host, "argv": argv, "detach": detach,
                             "pidfile": pidfile, "stdin_file": stdin_file,
                             "log": log})


def _action(raw: dict[str, Any], hosts: dict[str, Host], tunnels: dict[str, TunnelDef],
            *, where: str) -> Action | None:
    kinds = [k for k in ACTION_KINDS if k in raw]
    if len(kinds) > 1:
        raise ProfileError("multiple_actions", where)
    if not kinds:
        return None
    kind = kinds[0]
    body = raw[kind]
    if kind == "local":
        return _local_action(body, where=where)
    if kind == "remote":
        return _remote_action(body, hosts, where=where)
    if kind == "remote_signal":
        if not isinstance(body, dict) or body.get("host") not in hosts:
            raise ProfileError("bad_remote_signal", where)
        pidfile = _path(body.get("pidfile"), where=where)
        signal_ = str(body.get("signal", "TERM"))
        then_ = str(body.get("then", "KILL"))
        if signal_ not in REMOTE_SIGNALS or then_ not in REMOTE_SIGNALS:
            raise ProfileError("bad_remote_signal", where)
        grace_s = _num(body.get("grace_s", 10), where=where)
        if grace_s < 0:
            raise ProfileError("bad_remote_signal", where)
        return Action(kind, {"host": body["host"], "pidfile": pidfile,
                             "signal": signal_, "grace_s": grace_s, "then": then_})
    if kind in ("tunnel", "tunnel_close"):
        if body not in tunnels:
            raise ProfileError("unknown_tunnel", f"{where}: {body!r}")
        return Action(kind, {"id": body})
    if kind == "window_shot":
        if not isinstance(body.get("pgrep"), str):
            raise ProfileError("bad_window_shot", where)
        return Action(kind, {"pgrep": body["pgrep"]})
    if kind == "http":
        url = _loopback_url(body.get("url"), where=where)
        return Action(kind, {"url": url})
    raise ProfileError("bad_action", where)


def _check(raw: Any, hosts: dict[str, Host], tunnels: dict[str, TunnelDef], *, where: str) -> Check:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ProfileError("bad_check", where)
    kind, params = next(iter(raw.items()))
    if kind not in CHECK_KINDS:
        raise ProfileError("bad_check", f"{where}: {kind}")
    if kind == "all":
        return Check(kind, tuple(_check(c, hosts, tunnels, where=where) for c in params))
    if kind in CHECK_ALLOWED_KEYS:
        if not isinstance(params, dict):
            raise ProfileError("bad_check", where)
        extra = set(params) - CHECK_ALLOWED_KEYS[kind]
        if extra:
            raise ProfileError("unknown_key", f"{where}: {kind} {sorted(extra)}")
    if kind == "exit0":
        params = {"argv": _argv(params, where=where)}
        if not (os.path.isabs(params["argv"][0]) or params["argv"][0] in LOCAL_ARGV0_ALLOWLIST):
            raise ProfileError("argv0_not_absolute", where)
    if kind in ("http", "http_json"):
        _loopback_url(params.get("url") if isinstance(params, dict) else None, where=where)
    if kind == "file_mtime_newer" and "than" in params and params["than"] != "step_start":
        raise ProfileError("bad_check", f"{where}: than must be 'step_start'")
    if kind == "tunnel_up" and params not in tunnels:
        raise ProfileError("unknown_tunnel", where)
    if kind == "remote_pid_alive" and params.get("host") not in hosts:
        raise ProfileError("unknown_host", where)
    return Check(kind, params)


def _probe(raw: Any, hosts: dict[str, Host], *, where: str) -> Probe:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ProfileError("bad_probe", where)
    kind, params = next(iter(raw.items()))
    if kind not in PROBE_KINDS:
        raise ProfileError("bad_probe", f"{where}: {kind}")
    if kind.startswith("remote_"):
        if not isinstance(params, dict) or params.get("host") not in hosts:
            raise ProfileError("unknown_host", where)
        # `remote_stdout_advancing`/`remote_stdout_matches` are meaningless
        # without a command to run — fail closed rather than silently probing
        # nothing (or crashing at run time on a missing `argv`).
        if kind in ("remote_stdout_advancing", "remote_stdout_matches"):
            if "argv" not in params:
                raise ProfileError("bad_probe", f"{where}: {kind} requires argv")
            params = dict(params, argv=_argv(params["argv"], where=where))
        elif "argv" in params:
            params = dict(params, argv=_argv(params["argv"], where=where))
        if kind == "remote_stdout_matches":
            regex = params.get("regex")
            if not isinstance(regex, str):
                raise ProfileError("bad_probe", f"{where}: remote_stdout_matches requires regex")
            try:
                re.compile(regex)
            except re.error:
                raise ProfileError("bad_probe", f"{where}: bad regex {regex!r}") from None
        if kind == "remote_file_mtime_advancing":
            if "path" not in params:
                raise ProfileError("bad_probe", f"{where}: remote_file_mtime_advancing requires path")
            params = dict(params, path=_path(params["path"], where=where))
    if kind == "http":
        _loopback_url(params.get("url") if isinstance(params, dict) else None, where=where)
    return Probe(kind, params)


def _step(raw: Any, hosts, tunnels, *, where: str) -> Step:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise ProfileError("bad_step", where)
    extra = set(raw) - STEP_KEYS
    if extra:
        raise ProfileError("unknown_key", f"{where}: {sorted(extra)}")
    action = _action(raw, hosts, tunnels, where=where)
    check = _check(raw["check"], hosts, tunnels, where=where) if "check" in raw else None
    if action is None and check is None:
        raise ProfileError("empty_step", where)
    timeout = _num(raw.get("timeout_s", 30), where=where)
    if timeout <= 0:
        raise ProfileError("bad_timeout", where)
    lit = raw.get("evidence_literal")
    if lit is not None and not _TOKEN_RE.match(str(lit)):
        raise ProfileError("bad_evidence_literal", where)
    if lit is not None and check is None:
        # A literal is a CLAIM about the world after the step ran; only a CHECK
        # can substantiate one. An action alone reports whether the *command*
        # succeeded, which is a different question — `remote_signal` in
        # particular exits 0 when there was no pidfile to signal at all, so a
        # check-less `logoff_verified` would be a forged receipt for a logoff
        # that never happened. Fail closed at authoring time.
        raise ProfileError("literal_without_check", where)
    return Step(raw["name"], action, check, timeout, lit)


def _repo(raw: dict[str, Any], hosts, tunnels, *, where: str,
          project_exists_fn: Callable[[str], bool],
          launch_step_names: frozenset[str] = frozenset()) -> RepoDef:
    """One `repos:` entry. Deploy steps go through the *existing* `_step()`, so
    every Slice 1 argv rule (absolute argv0, banned tokens, no shell chars, the
    $SESSION_ID/$RUN_ID-only substitution) applies to them unchanged."""
    extra = set(raw) - REPO_KEYS
    if extra:
        raise ProfileError("unknown_key", f"{where}: {sorted(extra)}")

    rid = raw.get("id")                      # already shape-checked by the caller
    path = raw.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ProfileError("repo_path_not_absolute", f"{where}: {path!r}")
    repo_path = Path(path)
    if not repo_path.is_dir() or not (repo_path / ".git").exists():
        raise ProfileError("repo_path_missing", f"{where}: {path} is not a git checkout")

    project = raw.get("errorta_project")
    if not isinstance(project, str) or not _TOKEN_RE.match(project):
        raise ProfileError("unknown_errorta_project", f"{where}: {project!r}")
    # Resolve it NOW: a profile naming a project that does not exist would fail
    # at fix-cycle time, minutes after a stall, with the run already torn down.
    if not project_exists_fn(project):
        raise ProfileError("unknown_errorta_project", f"{where}: {project}")

    fixable = raw.get("fixable", True)
    if not isinstance(fixable, bool):
        raise ProfileError("bad_repo", f"{where}: fixable must be a bool")

    classify_raw = raw.get("classify") or []
    if not isinstance(classify_raw, list) or not all(isinstance(c, str) for c in classify_raw):
        raise ProfileError("bad_repo", f"{where}: classify must be a list of strings")
    for c in classify_raw:
        if c.startswith(LAUNCH_STEP_CLASS_PREFIX):
            # Resolved against THIS profile's launch steps at load time: a typo
            # here would otherwise be a class that silently never fires, and the
            # cycle it should have attributed would pause as ambiguous.
            step_name = c[len(LAUNCH_STEP_CLASS_PREFIX):]
            if step_name not in launch_step_names:
                raise ProfileError("unknown_launch_step", f"{where}: {c!r}")
            continue
        if c not in EVIDENCE_CLASSES:
            raise ProfileError("unknown_evidence_class", f"{where}: {c!r}")

    deploy_raw = raw.get("deploy") or []
    if not isinstance(deploy_raw, list):
        raise ProfileError("bad_deploy_step", f"{where}: deploy must be a list")
    deploy: list[Step] = []
    for i, s in enumerate(deploy_raw):
        step = _step(s, hosts, tunnels, where=f"{where}.deploy[{i}]")
        if step.action is None or step.action.kind not in DEPLOY_ACTION_KINDS:
            kind = step.action.kind if step.action else None
            raise ProfileError("bad_deploy_step", f"{where}.deploy[{i}]: {kind!r}")
        deploy.append(step)

    return RepoDef(str(rid), path, project, fixable, tuple(classify_raw), tuple(deploy))


def _fix_loop(raw: Any, repos: tuple[RepoDef, ...]) -> FixLoop:
    if not isinstance(raw, dict):
        raise ProfileError("bad_fix_loop", repr(raw)[:80])
    extra = set(raw) - FIX_LOOP_KEYS
    if extra:
        raise ProfileError("unknown_key", f"fix_loop: {sorted(extra)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ProfileError("bad_fix_loop", "enabled must be a bool")

    values: dict[str, int] = {}
    for k, default in FIX_CAP_DEFAULTS.items():
        v = int(_num(raw.get(k, default), where=f"fix_loop.{k}"))
        # Caps may only ever be LOWERED below the shipped default (mirrors _caps).
        if v > default:
            raise ProfileError("cap_raised", f"fix_loop.{k}={v} (default {default})")
        if v <= 0:
            raise ProfileError("bad_fix_loop", f"fix_loop.{k}={v}")
        values[k] = v
    # A hang detector below the CLI's own per-turn timeout would cancel a run
    # every time one turn thought hard. Not a cap -- a correctness floor.
    if values["idle_timeout_s"] <= MIN_IDLE_TIMEOUT_S:
        raise ProfileError("idle_below_turn_timeout",
                           f"idle_timeout_s={values['idle_timeout_s']} <= {MIN_IDLE_TIMEOUT_S}")

    route = raw.get("triage_route", TRIAGE_ROUTES[0])
    if route not in TRIAGE_ROUTES:
        raise ProfileError("bad_triage_route", repr(route))

    dev_route = raw.get("dev_route", "claude_cli.opus")
    if not isinstance(dev_route, str) or not _DEV_ROUTE_RE.fullmatch(dev_route):
        raise ProfileError("bad_dev_route", repr(dev_route)[:80])

    if enabled and not any(r.fixable for r in repos):
        raise ProfileError("fix_loop_without_repos",
                           "fix_loop.enabled needs at least one fixable repo")
    return FixLoop(enabled=enabled, triage_route=str(route), dev_route=dev_route, **values)


def _caps(raw: Any) -> Caps:
    if raw is None:
        return DEFAULT_CAPS
    if not isinstance(raw, dict):
        raise ProfileError("bad_caps")
    values: dict[str, int] = {}
    for k in ("max_launches_per_hour", "min_launch_gap_s", "max_launches_per_day",
              "max_consecutive_failed_cycles"):
        if k in raw:
            v = int(_num(raw[k], where=f"caps.{k}"))
            d = getattr(DEFAULT_CAPS, k)
            looser = v < d if k == "min_launch_gap_s" else v > d
            if looser:
                raise ProfileError("cap_above_default", f"{k}={v} (default {d})")
            values[k] = v
    extra = set(raw) - set(values)
    if extra:
        raise ProfileError("unknown_key", f"caps: {sorted(extra)}")
    return Caps(**values)


def _file_guard(path: Path) -> None:
    if path.is_symlink():
        raise ProfileError("profile_is_symlink", str(path))
    try:
        path.resolve().relative_to(profiles_dir().resolve())
    except ValueError:
        raise ProfileError("profile_outside_dir", str(path)) from None
    st = path.stat()
    if st.st_uid != os.getuid():
        raise ProfileError("profile_not_owned", str(path))
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o644, 0o640):
        raise ProfileError("profile_mode_insecure", oct(stat.S_IMODE(st.st_mode)))


def _build_profile(path: Path, doc: dict[str, Any], known_hosts_fn: Callable[[str], bool],
                   project_exists_fn: Callable[[str], bool] = default_project_exists) -> Profile:
    extra = set(doc) - TOP_KEYS
    if extra:
        raise ProfileError("unknown_key", str(sorted(extra)))
    if doc.get("version") != 1:
        raise ProfileError("unsupported_version", repr(doc.get("version")))
    if doc.get("created_by") != "operator":
        raise ProfileError("created_by_not_operator", repr(doc.get("created_by")))

    hosts_raw = doc.get("hosts") or {}
    if not isinstance(hosts_raw, dict):
        raise ProfileError("bad_host", "hosts must be a mapping")
    hosts: dict[str, Host] = {}
    for hid, h in hosts_raw.items():
        if not isinstance(h, dict) or not _TOKEN_RE.match(str(h.get("ssh_host", ""))):
            raise ProfileError("bad_host", str(hid))
        if not known_hosts_fn(str(h["ssh_host"])):
            raise ProfileError("host_unknown", f"{hid}: {h['ssh_host']} not in known_hosts")
        hosts[str(hid)] = Host(str(h["ssh_host"]), h.get("ssh_port"), h.get("ssh_username"),
                               h.get("ssh_key_path"))

    tunnels_raw = doc.get("tunnels") or []
    if not isinstance(tunnels_raw, list):
        raise ProfileError("bad_tunnel", "tunnels must be a list")
    tunnels: dict[str, TunnelDef] = {}
    for t in tunnels_raw:
        if not isinstance(t, dict) or t.get("host") not in hosts or not t.get("reverse"):
            raise ProfileError("bad_tunnel", str(t))
        pairs = tuple((int(_num(p["remote_port"], where="tunnels")),
                       int(_num(p["local_port"], where="tunnels"))) for p in t["reverse"])
        tunnels[str(t["id"])] = TunnelDef(str(t["id"]), str(t["host"]), pairs)

    launch = tuple(_step(s, hosts, tunnels, where=f"launch[{i}]") for i, s in enumerate(doc.get("launch") or []))
    evidence = tuple(_step(s, hosts, tunnels, where=f"evidence[{i}]") for i, s in enumerate(doc.get("evidence") or []))
    teardown = tuple(_step(s, hosts, tunnels, where=f"teardown[{i}]") for i, s in enumerate(doc.get("teardown") or []))
    if not any(s.evidence_literal == "logoff_verified" for s in teardown):
        raise ProfileError("missing_logoff_literal")

    watch: list[WatchProbe] = []
    for i, w in enumerate(doc.get("watch") or []):
        if not isinstance(w, dict) or not _TOKEN_RE.match(str(w.get("id", ""))):
            raise ProfileError("bad_watch", f"watch[{i}]")
        on_stall = str(w.get("on_stall", "stop"))
        if on_stall not in ("stop", "warn"):
            raise ProfileError("bad_on_stall", f"watch[{i}]: {on_stall}")
        watch.append(WatchProbe(str(w["id"]), _num(w.get("every_s", 30), where=f"watch[{i}]"),
                                _num(w.get("stall_after_s", 0), where=f"watch[{i}]"), on_stall,
                                _probe(w.get("probe"), hosts, where=f"watch[{i}]")))

    repos_raw = doc.get("repos")
    repos: tuple[RepoDef, ...] = ()
    if repos_raw is not None:
        if not isinstance(repos_raw, list) or not repos_raw:
            raise ProfileError("bad_repo", "repos must be a non-empty list")
        seen: set[str] = set()
        for i, r in enumerate(repos_raw):
            if not isinstance(r, dict):
                raise ProfileError("bad_repo", f"repos[{i}]")
            rid = r.get("id")
            # Ids are checked across the whole list FIRST so a duplicate is
            # reported as a duplicate, not as whatever the copy-pasted entry's
            # next-worst field happens to be.
            if not isinstance(rid, str) or not _REPO_ID_RE.match(rid):
                raise ProfileError("bad_repo_id", f"repos[{i}]: {rid!r}")
            if rid in seen:
                raise ProfileError("duplicate_repo_id", rid)
            seen.add(rid)
        launch_names = frozenset(s.name for s in launch)
        repos = tuple(_repo(r, hosts, tunnels, where=f"repos[{i}]",
                            project_exists_fn=project_exists_fn,
                            launch_step_names=launch_names)
                      for i, r in enumerate(repos_raw))
        claimed: dict[str, str] = {}
        for r in repos:
            for c in r.classify:
                if c in claimed:
                    raise ProfileError("ambiguous_class_mapping",
                                       f"{c}: {claimed[c]} and {r.id}")
                claimed[c] = r.id

    fix_loop = None
    if "fix_loop" in doc:
        fix_loop = _fix_loop(doc.get("fix_loop"), repos)
        if fix_loop.enabled and not repos:
            raise ProfileError("fix_loop_without_repos", "fix_loop.enabled needs repos")

    bans = tuple(str(b) for b in (doc.get("ban_signals") or []))
    for b in bans:
        try:
            re.compile(b)
        except re.error:
            raise ProfileError("bad_ban_regex", b) from None

    return Profile(name=path.stem, hosts=hosts, tunnels=tunnels, launch=launch,
                   watch=tuple(watch), evidence=evidence, teardown=teardown,
                   caps=_caps(doc.get("caps")), ban_signals=bans, repos=repos,
                   fix_loop=fix_loop)


def load_profile(path: Path, *, known_hosts_fn: Callable[[str], bool] = default_known_hosts_check,
                 project_exists_fn: Callable[[str], bool] = default_project_exists) -> Profile:
    path = Path(path)
    _file_guard(path)
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ProfileError("yaml_invalid", str(exc)) from None
    if not isinstance(doc, dict):
        raise ProfileError("not_a_mapping")
    try:
        return _build_profile(path, doc, known_hosts_fn, project_exists_fn)
    except ProfileError:
        raise
    except Exception as exc:  # last line of defence: any wrong-shaped YAML we
        # didn't anticipate must still fail closed as a ProfileError, not crash
        # the caller (e.g. list_profiles scanning a directory of profiles).
        raise ProfileError("profile_malformed", repr(exc)[:200]) from None


def list_profiles(*, known_hosts_fn: Callable[[str], bool] = default_known_hosts_check,
                  project_exists_fn: Callable[[str], bool] = default_project_exists) -> list[dict[str, Any]]:
    d = profiles_dir()
    if not d.is_dir():
        return []
    rows = []
    for f in sorted(d.glob("*.yaml")):
        try:
            load_profile(f, known_hosts_fn=known_hosts_fn, project_exists_fn=project_exists_fn)
            rows.append({"name": f.stem, "valid": True, "error": None})
        except ProfileError as exc:
            rows.append({"name": f.stem, "valid": False, "error": exc.code})
    return rows


__all__ = ["Profile", "ProfileError", "Step", "WatchProbe", "Caps", "DEFAULT_CAPS", "Host",
           "TunnelDef", "Check", "Probe", "Action", "RepoDef", "FixLoop", "load_profile",
           "list_profiles", "profiles_dir", "default_project_exists", "BANNED_TOKENS",
           "LOCAL_ARGV0_ALLOWLIST", "BRAIN_REQUIRED_FLAGS", "EVIDENCE_CLASSES",
           "TRIAGE_ROUTES", "DEPLOY_ACTION_KINDS", "FIX_CAP_DEFAULTS", "MIN_IDLE_TIMEOUT_S"]
