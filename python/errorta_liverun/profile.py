"""Profile schema + fail-closed validator (spec §3.2)."""
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
PROBE_KINDS = {"http", "remote_pid_alive", "remote_file_mtime_advancing",
               "remote_stdout_advancing", "remote_stdout_matches", "elapsed_lt_s"}
ACTION_KINDS = {"local", "remote", "remote_signal", "tunnel", "tunnel_close", "window_shot", "http"}
TOP_KEYS = {"version", "created_by", "hosts", "tunnels", "launch", "watch", "evidence",
            "teardown", "caps", "ban_signals"}
STEP_KEYS = {"name", "check", "timeout_s", "evidence_literal"} | ACTION_KINDS


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


def profiles_dir() -> Path:
    return errorta_home() / "liverun" / "profiles"


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
    if kind == "exit0":
        params = {"argv": _argv(params, where=where)}
        if not (os.path.isabs(params["argv"][0]) or params["argv"][0] in LOCAL_ARGV0_ALLOWLIST):
            raise ProfileError("argv0_not_absolute", where)
    if kind in ("http", "http_json"):
        _loopback_url(params.get("url") if isinstance(params, dict) else None, where=where)
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
        if params.get("host") not in hosts:
            raise ProfileError("unknown_host", where)
        if "argv" in params:
            params = dict(params, argv=_argv(params["argv"], where=where))
        if kind == "remote_file_mtime_advancing" and "path" in params:
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
    return Step(raw["name"], action, check, timeout, lit)


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


def _build_profile(path: Path, doc: dict[str, Any], known_hosts_fn: Callable[[str], bool]) -> Profile:
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

    bans = tuple(str(b) for b in (doc.get("ban_signals") or []))
    for b in bans:
        try:
            re.compile(b)
        except re.error:
            raise ProfileError("bad_ban_regex", b) from None

    return Profile(name=path.stem, hosts=hosts, tunnels=tunnels, launch=launch,
                   watch=tuple(watch), evidence=evidence, teardown=teardown,
                   caps=_caps(doc.get("caps")), ban_signals=bans)


def load_profile(path: Path, *, known_hosts_fn: Callable[[str], bool] = default_known_hosts_check) -> Profile:
    path = Path(path)
    _file_guard(path)
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ProfileError("yaml_invalid", str(exc)) from None
    if not isinstance(doc, dict):
        raise ProfileError("not_a_mapping")
    try:
        return _build_profile(path, doc, known_hosts_fn)
    except ProfileError:
        raise
    except Exception as exc:  # last line of defence: any wrong-shaped YAML we
        # didn't anticipate must still fail closed as a ProfileError, not crash
        # the caller (e.g. list_profiles scanning a directory of profiles).
        raise ProfileError("profile_malformed", repr(exc)[:200]) from None


def list_profiles(*, known_hosts_fn: Callable[[str], bool] = default_known_hosts_check) -> list[dict[str, Any]]:
    d = profiles_dir()
    if not d.is_dir():
        return []
    rows = []
    for f in sorted(d.glob("*.yaml")):
        try:
            load_profile(f, known_hosts_fn=known_hosts_fn)
            rows.append({"name": f.stem, "valid": True, "error": None})
        except ProfileError as exc:
            rows.append({"name": f.stem, "valid": False, "error": exc.code})
    return rows


__all__ = ["Profile", "ProfileError", "Step", "WatchProbe", "Caps", "DEFAULT_CAPS", "Host",
           "TunnelDef", "Check", "Probe", "Action", "load_profile", "list_profiles",
           "profiles_dir", "BANNED_TOKENS", "LOCAL_ARGV0_ALLOWLIST", "BRAIN_REQUIRED_FLAGS"]
