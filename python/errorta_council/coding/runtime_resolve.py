"""F101-03 S1 — the universal Run front door's *grounded* launch resolver.

`resolve_launch_plan` answers one question for the universal Run button: "how do
I start this project, and is that start real?" It returns a `LaunchPlan` (a
concrete, grounded way to run the project) or `Unresolved` (a structured "I don't
know how to run this, here's what I looked for") — it never guesses a command.

The load-bearing invariant is **grounded-or-refuse** (spec D2): a plan is only
returnable if its ``start`` entrypoint exists on the worktree head. Because the
master worktree on disk *is* the head, existence is checked against the workspace
files directly (no git plumbing) — the same read-only discipline `runtime.detect`
already uses. ``head`` rides along for provenance only.

This kills the reddit-clone class: a stored ``runtime-profile`` (or a detector
proposal) advertising ``npm run dev`` for code where ``package.json`` / that
script does not exist is discarded, not executed.

Precedence (first grounded candidate wins):

1. a stored runtime profile,
2. detection (``runtime.detect``),
3. *(forward hook)* a PM deliverable manifest — not a grounding source yet,
4. else ``Unresolved`` with the checklist the panel shows.

PURE + read-only, stdlib-only imports plus ``.runtime`` (Council invariant 3): it
reads files and profiles; it never spawns a process, opens a socket, or wraps a
sandbox. Executing a resolved plan is the launcher seam's job
(``runtime_launchers``), driven by the ``/runtime/run`` route.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runtime import RuntimeProfile, RuntimeProfileStore, detect

# The coarse profile ``kind`` -> the universal-Run ``modality`` (``launch_kind``)
# that selects a Launcher. S1 maps ONLY the modalities it can actually run today
# (existing static/server/cli behavior). ``desktop``/``container``/``unknown`` are
# intentionally absent — a candidate of those kinds falls through with a
# checklist note rather than resolving to a plan Run cannot honor. Later slices
# add the kind -> modality rows together with their launchers.
_MODALITY_BY_KIND: dict[str, str] = {
    "static": "static",
    "web": "server",
    "api": "server",
    "cli": "cli",
    # F101-03 S2: a GUI app that opens its own OS window (sandboxed-windowed, T1).
    "desktop": "desktop",
    # F101-03 S4: a compiled native executable (host os/arch-gated).
    "binary": "binary",
    # F101-03 S6: a Docker image (the container is the isolation).
    "container": "container",
}

# Trust tiers: T0 = the existing F039 sandbox (headless); T1 = sandboxed but
# windowed (a GUI reaches the OS window server). T2 (consent-gated reduced
# isolation) is decided at run time when no windowing sandbox is available.
TRUST_TIER_T0 = 0
TRUST_TIER_T1 = 1

# The minimum viable trust tier a modality's plan previews (run time may raise
# it to T2 if the sandbox can't host a window — shown before Run either way).
_MIN_TIER_BY_MODALITY: dict[str, int] = {"desktop": TRUST_TIER_T1}

# argv[0] tokens that are interpreters/launchers, never a repo entrypoint.
_INTERPRETERS = frozenset({
    "python", "python3", "py", "node", "nodejs", "deno", "bun", "ruby", "go",
    "sh", "bash", "zsh", "uv", "poetry", "pipenv", "pdm", "hatch",
    "flask", "uvicorn", "gunicorn", "hypercorn", "waitress", "streamlit",
})
# Node package managers that resolve a run *script* out of package.json.
_NODE_PM = frozenset({"npm", "pnpm", "yarn"})
# Node binary runners (run a local/installed bin) — grounded by package.json.
_NODE_EXEC = frozenset({"npx", "bunx"})
# ``python -m <module>`` where the module is stdlib/framework, not a repo file —
# so ``-m`` imposes no repo-entrypoint requirement (e.g. static's http.server).
_STDLIB_MODULES = frozenset({
    "http.server", "https.server", "flask", "uvicorn", "gunicorn", "hypercorn",
    "waitress", "pytest", "unittest", "venv", "pip", "build", "streamlit",
    "django",
})
_SOURCE_EXTS = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".rb", ".go",
    ".rs", ".php", ".pl", ".lua",
})
_NPM_BARE_SUBCOMMANDS = frozenset({
    "start", "dev", "build", "test", "serve", "preview",
})
# F101-03 S4: build-then-run tools grounded on their manifest (the runnable
# artifact is produced by the build, so D2's file-exists check applies here).
_BUILD_TOOLS = {"cargo": "Cargo.toml", "go": "go.mod", "make": "Makefile"}
# F101-03: game engines run a project via an engine binary + a root manifest —
# grounded on that manifest (the engine binary itself is a host-PATH dependency,
# resolved at spawn, the same as the build tools above).
_ENGINE_MANIFESTS = {"godot": "project.godot"}
_COMPOSE_FILES = (
    "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml",
)


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LaunchPlan:
    """A concrete, grounded way to run a project. ``source_profile`` is the
    ``RuntimeProfile`` the plan was grounded from (so the route can persist it
    before dispatch); it is NOT serialized into the wire projection."""
    modality: str
    profile_id: str
    kind: str
    start: list[str]
    setup: list[list[str]]
    working_dir: str
    ports: list[dict[str, Any]]
    health: dict[str, Any]
    env_required: list[str]
    grounded_by: str            # "profile" | "detector" | "pm_manifest"
    verified_paths: list[str]   # files proven to exist at head
    head: str = ""
    trust_tier: int = TRUST_TIER_T0
    warnings: list[str] = field(default_factory=list)
    host_requirements: dict[str, str] | None = None  # {os, arch} for a binary
    source_profile: RuntimeProfile | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "modality": self.modality,
            "launch_kind": self.modality,
            "profile_id": self.profile_id,
            "kind": self.kind,
            "start": list(self.start),
            "setup": [list(s) for s in self.setup],
            "working_dir": self.working_dir,
            "ports": [dict(p) for p in self.ports],
            "health": dict(self.health),
            "env_required": list(self.env_required),
            "grounded_by": self.grounded_by,
            "verified_paths": list(self.verified_paths),
            "head": self.head,
            "trust_tier": self.trust_tier,
            "host": "local sidecar",
            "host_requirements": dict(self.host_requirements) if self.host_requirements else None,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Unresolved:
    """No grounded way to run the project. ``looked_for`` is the human-readable
    checklist the panel renders (including why any candidate was rejected)."""
    looked_for: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"looked_for": list(self.looked_for)}


# --------------------------------------------------------------------------- #
# Grounding — the entrypoint-exists check that makes a plan trustworthy.
# --------------------------------------------------------------------------- #
def _basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _first_glob(work_dir: Path, patterns: tuple[str, ...]) -> str | None:
    """The name of the first file matching any of ``patterns`` under ``work_dir``
    (pattern order, then name order), or None. Read-only."""
    for pat in patterns:
        for p in sorted(work_dir.glob(pat)):
            if p.is_file():
                return p.name
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _safe_join(work_dir: Path, token: str) -> Path | None:
    """Resolve a repo-relative token under ``work_dir``, refusing absolute paths
    and anything that escapes the working dir (defense-in-depth; profiles are
    already worktree-relative-validated)."""
    if token.startswith("/") or token.startswith("\\") or ".." in Path(token).parts:
        return None
    # Resolve ``work_dir`` too, so a symlink anywhere in the workspace path (e.g.
    # macOS ``/var/folders`` -> ``/private/...``, a symlinked ``ERRORTA_HOME`` or
    # home dir) doesn't defeat the ``is_relative_to`` containment check and make a
    # present entrypoint read as missing. Matches the executor, which already
    # ``.resolve()``s its roots (``RuntimeProcessManager._resolve_working_dir``).
    base = work_dir.resolve()
    candidate = (base / token).resolve()
    if candidate == base or candidate.is_relative_to(base):
        return candidate
    return None


def _has_source_ext(token: str) -> bool:
    return Path(token).suffix.lower() in _SOURCE_EXTS


def _looks_like_repo_path(token: str) -> bool:
    if "://" in token or "{" in token or "}" in token:
        return False
    if _has_source_ext(token):
        return True
    return "/" in token and not token.startswith("-")


def _npm_script(start: list[str]) -> str | None:
    """The run-script name a package-manager argv resolves, or None.

    ``npm run <s>`` / ``pnpm run <s>`` / ``yarn run <s>`` -> ``<s>``;
    ``npm start`` / ``yarn dev`` (a bare known subcommand) -> that subcommand."""
    if len(start) < 2:
        return None
    if start[1] == "run":
        return start[2] if len(start) > 2 else None
    if start[1] in _NPM_BARE_SUBCOMMANDS:
        return start[1]
    return None


def _ground_module(module: str, work_dir: Path,
                   verified: list[str], missing: list[str]) -> None:
    if module in _STDLIB_MODULES:
        return  # a stdlib/framework runner imposes no repo-entrypoint requirement
    if module == ".":
        if (work_dir / "__main__.py").exists():
            verified.append("__main__.py")
        else:
            missing.append("__main__.py")
        return
    rel = module.replace(".", "/")
    if (work_dir / f"{rel}.py").exists():
        verified.append(f"{rel}.py")
    elif (work_dir / rel / "__init__.py").exists():
        verified.append(f"{rel}/__init__.py")
    else:
        missing.append(f"{rel}.py")


def ground_start(start: list[str], work_dir: Path) -> tuple[list[str], list[str]]:
    """Return ``(verified_paths, missing_paths)`` for a ``start`` argv.

    ``verified`` are repo entrypoints proven to exist under ``work_dir``;
    ``missing`` are named-but-absent entrypoints (any non-empty ``missing`` means
    the plan is NOT grounded). An empty ``(verified, missing)`` is "opaque" — no
    repo entrypoint could be identified (e.g. a stdlib ``python -m http.server``
    static server); the resolver applies a modality-specific fallback.
    """
    verified: list[str] = []
    missing: list[str] = []
    if not start:
        return verified, missing

    exe = _basename(start[0]).lower()

    # Node: package.json is the grounding artifact; a run-script must exist.
    if exe in _NODE_PM or exe in _NODE_EXEC:
        pkg = work_dir / "package.json"
        if not pkg.exists():
            missing.append("package.json")
            return verified, missing
        verified.append("package.json")
        if exe in _NODE_PM:
            script = _npm_script(start)
            if script is not None:
                data = _read_json(pkg)
                scripts = data.get("scripts")
                scripts = scripts if isinstance(scripts, dict) else {}
                if script not in scripts:
                    missing.append(f"package.json#scripts.{script}")
        return verified, missing

    # Container (F101-03 S6): ground on the Dockerfile / compose file.
    if exe == "docker":
        compose = "compose" in start[:2]
        if compose:
            found = next((c for c in _COMPOSE_FILES if (work_dir / c).exists()), None)
            if found:
                verified.append(found)
            else:
                missing.append("a compose file")
        elif (work_dir / "Dockerfile").exists():
            verified.append("Dockerfile")
        else:
            missing.append("Dockerfile")
        return verified, missing

    # Game engines (godot): ground on the engine's root project manifest.
    if exe in _ENGINE_MANIFESTS:
        manifest = _ENGINE_MANIFESTS[exe]
        if (work_dir / manifest).exists():
            verified.append(manifest)
        else:
            missing.append(manifest)
        return verified, missing

    # LÖVE (love2d): the game's entrypoint is main.lua.
    if exe == "love":
        if (work_dir / "main.lua").exists():
            verified.append("main.lua")
        else:
            missing.append("main.lua")
        return verified, missing

    # PHP: the built-in server serves index.php; Laravel serves via artisan.
    if exe == "php":
        target = "artisan" if "artisan" in start else "index.php"
        if (work_dir / target).exists():
            verified.append(target)
        else:
            missing.append(target)
        return verified, missing

    # .NET: ground on the explicit ``--project`` target if given, else any
    # C#/F# project or solution manifest.
    if exe == "dotnet":
        if "--project" in start:
            idx = start.index("--project")
            target = start[idx + 1] if idx + 1 < len(start) else ""
            resolved = _safe_join(work_dir, target) if target else None
            if resolved is not None and resolved.exists():
                verified.append(target)
            else:
                missing.append(target or "a project after --project")
        elif _first_glob(work_dir, ("*.csproj", "*.fsproj", "*.sln")) is not None:
            verified.append(_first_glob(work_dir, ("*.csproj", "*.fsproj", "*.sln")))
        else:
            missing.append("a .csproj / .fsproj / .sln")
        return verified, missing

    # Ruby Rack (rackup): the runnable rack app is config.ru.
    if exe == "rackup":
        if (work_dir / "config.ru").exists():
            verified.append("config.ru")
        else:
            missing.append("config.ru")
        return verified, missing

    # Deno's `task` form runs a named task from the deno config; `deno run <file>`
    # falls through to the generic scan below (its entry has a source extension).
    if exe == "deno" and len(start) > 1 and start[1] == "task":
        cfg = _first_glob(work_dir, ("deno.json", "deno.jsonc"))
        if cfg is None:
            missing.append("deno.json")
            return verified, missing
        verified.append(cfg)
        # Verify the named task is actually declared (parity with the npm-script
        # grounding, which checks package.json#scripts.<name> exists).
        task = start[2] if len(start) > 2 else None
        if task is not None:
            data = _read_json(work_dir / cfg)
            tasks = data.get("tasks")
            tasks = tasks if isinstance(tasks, dict) else {}
            if task not in tasks:
                missing.append(f"deno.json#tasks.{task}")
        return verified, missing

    # Build-then-run tools (cargo/go/make): ground on the manifest.
    if exe in _BUILD_TOOLS:
        manifest = _BUILD_TOOLS[exe]
        if (work_dir / manifest).exists() or (
                exe == "make" and (work_dir / "makefile").exists()):
            verified.append(manifest)
        else:
            missing.append(manifest)
        return verified, missing

    # Generic interpreter argv: scan for `-m module` + repo-path tokens.
    module_next = False
    for idx, token in enumerate(start):
        if module_next:
            module_next = False
            _ground_module(token, work_dir, verified, missing)
            continue
        if token == "-m":
            module_next = True
            continue
        if token.startswith("-"):
            continue
        base = _basename(token).lower()
        if base in _INTERPRETERS:
            continue
        if idx == 0:
            # argv[0] wasn't a known interpreter and isn't `-m`; only treat it as
            # an entrypoint if it looks like a repo path (e.g. `./run.sh`).
            if not _looks_like_repo_path(token):
                continue
        if _looks_like_repo_path(token):
            resolved = _safe_join(work_dir, token)
            if resolved is not None and resolved.exists():
                verified.append(token)
            elif _has_source_ext(token):
                missing.append(token)
    return verified, missing


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
def _host_requirements(profile: RuntimeProfile) -> dict[str, str] | None:
    """The ``{os, arch}`` a binary profile needs (round-tripped via ``_extras``
    from the detector's header sniff), or None for a non-binary profile."""
    req = profile._extras.get("host_requirements")
    if isinstance(req, dict) and req.get("os") and req.get("arch"):
        return {"os": str(req["os"]), "arch": str(req["arch"])}
    return None


def _ordered_profiles(rstore: RuntimeProfileStore) -> list[RuntimeProfile]:
    """Stored profiles with ``default`` first (the conventional primary)."""
    profiles = rstore.list_profiles()
    return sorted(profiles, key=lambda p: (p.profile_id != "default", p.profile_id))


def _plan_from_profile(
    profile: RuntimeProfile, root: Path, *, grounded_by: str, head: str,
    looked_for: list[str],
) -> LaunchPlan | None:
    """Build a grounded LaunchPlan from a profile, or None (appending a reason to
    ``looked_for``) if it isn't runnable-via-Run or isn't grounded."""
    modality = _MODALITY_BY_KIND.get(profile.kind)
    if modality is None:
        looked_for.append(
            f"{grounded_by} '{profile.profile_id}': kind '{profile.kind}' is not "
            f"runnable from Run yet")
        return None
    if not profile.start:
        looked_for.append(
            f"{grounded_by} '{profile.profile_id}': no start command")
        return None

    if profile.working_dir in (".", ""):
        work_dir: Path | None = root
    else:
        work_dir = _safe_join(root, profile.working_dir)
    if work_dir is None:
        looked_for.append(
            f"{grounded_by} '{profile.profile_id}': working_dir escapes the workspace")
        return None

    verified, missing = ground_start(profile.start, work_dir)
    if not verified and not missing:
        # Opaque: no repo entrypoint identifiable. Static's real deliverable is
        # index.html (served over http.server); anything else we cannot ground,
        # so we refuse rather than guess.
        if profile.kind == "static":
            if (work_dir / "index.html").exists():
                verified = ["index.html"]
            else:
                missing = ["index.html"]
        else:
            looked_for.append(
                f"{grounded_by} '{profile.profile_id}': could not verify an "
                f"entrypoint for `{' '.join(profile.start)}`")
            return None
    if missing:
        looked_for.append(
            f"{grounded_by} '{profile.profile_id}': entrypoint not found on "
            f"master ({', '.join(missing)})")
        return None

    return LaunchPlan(
        modality=modality,
        profile_id=profile.profile_id,
        kind=profile.kind,
        start=list(profile.start),
        setup=[list(s) for s in profile.setup],
        working_dir=profile.working_dir or ".",
        ports=[dict(p) for p in profile.ports],
        health=dict(profile.health),
        env_required=list(profile.env_required),
        grounded_by=grounded_by,
        verified_paths=verified,
        head=head,
        trust_tier=_MIN_TIER_BY_MODALITY.get(modality, TRUST_TIER_T0),
        warnings=list(profile.safety_warnings),
        host_requirements=_host_requirements(profile),
        source_profile=profile,
    )


def _default_looked_for(root: Path) -> list[str]:
    return [
        "a saved runtime profile whose start entrypoint exists on master",
        "package.json with a runnable script (Node); deno.json (Deno)",
        "app.py / main.py / __main__.py (Python)",
        "a game manifest: project.godot (Godot) / main.lua (LÖVE)",
        "a web/CLI entrypoint: Gemfile or bin/rails (Ruby), index.php or "
        "artisan (PHP), a .csproj/.sln (.NET), Cargo.toml/go.mod (Rust/Go)",
        "index.html (static site)",
    ]


def resolve_launch_plan(
    workspace_root: str | Path, head: str, rstore: RuntimeProfileStore,
    project_id: str,
) -> LaunchPlan | Unresolved:
    """Resolve the grounded way to run a project, or ``Unresolved``.

    Precedence: stored profile -> detection -> (PM manifest, forward hook) ->
    unresolved. Every candidate is grounded (its start entrypoint must exist on
    the worktree head) before it can win; ungrounded candidates are skipped with
    a checklist note, never executed.
    """
    root = Path(workspace_root)
    looked_for: list[str] = []

    # 1. Stored profiles (default first).
    for profile in _ordered_profiles(rstore):
        plan = _plan_from_profile(
            profile, root, grounded_by="profile", head=head, looked_for=looked_for)
        if plan is not None:
            return plan

    # 2. Detection.
    for profile in detect(root, project_id=project_id):
        plan = _plan_from_profile(
            profile, root, grounded_by="detector", head=head, looked_for=looked_for)
        if plan is not None:
            return plan

    # 3. PM deliverable manifest (F100) — declared grounding source; not wired as
    #    one yet (a later slice). No candidate contributed here in S1.

    # 4. Unresolved. Show the specific rejection reasons if we saw candidates,
    #    else the generic "what we looked for" checklist.
    checklist = _dedupe(looked_for) or _default_looked_for(root)
    return Unresolved(looked_for=checklist)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = [
    "LaunchPlan",
    "Unresolved",
    "TRUST_TIER_T0",
    "TRUST_TIER_T1",
    "ground_start",
    "resolve_launch_plan",
]
