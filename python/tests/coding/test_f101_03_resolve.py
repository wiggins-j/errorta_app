"""F101-03 S1 — the grounded launch resolver.

Locks the marquee invariant: `resolve_launch_plan` only returns a `LaunchPlan`
whose start entrypoint exists on the worktree (grounded-or-refuse, spec D2), and
otherwise an `Unresolved` checklist — it never guesses. Pure/read-only: these
tests build a workspace dir + a `RuntimeProfileStore` directly (no manager, no
subprocess).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from errorta_council.coding.runtime import RuntimeProfile, RuntimeProfileStore
from errorta_council.coding.runtime_resolve import (
    LaunchPlan,
    Unresolved,
    ground_start,
    resolve_launch_plan,
)


def _store(tmp_path: Path) -> RuntimeProfileStore:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    return RuntimeProfileStore(ledger_dir, threading.Lock())


def _profile(**over) -> RuntimeProfile:
    base = dict(
        profile_id="default", project_id="p", kind="api",
        runtime_mode="managed_local", working_dir=".",
        setup=[], start=["python", "app.py"],
    )
    base.update(over)
    return RuntimeProfile(**base)


# --------------------------------------------------------------------------- #
# ground_start — the entrypoint-exists check.
# --------------------------------------------------------------------------- #
def test_ground_python_path_present(tmp_path: Path):
    (tmp_path / "app.py").write_text("x=1\n")
    verified, missing = ground_start(["python", "app.py"], tmp_path)
    assert verified == ["app.py"] and missing == []


def test_ground_python_path_absent(tmp_path: Path):
    verified, missing = ground_start(["python", "app.py"], tmp_path)
    assert verified == [] and missing == ["app.py"]


def test_ground_npm_script_present(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    verified, missing = ground_start(["npm", "run", "dev"], tmp_path)
    assert "package.json" in verified and missing == []


def test_ground_npm_script_absent_is_the_reddit_case(tmp_path: Path):
    # package.json exists but has no `dev` script — the hallucinated-command class.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    verified, missing = ground_start(["npm", "run", "dev"], tmp_path)
    assert missing == ["package.json#scripts.dev"]


def test_ground_npm_no_package_json(tmp_path: Path):
    verified, missing = ground_start(["npm", "run", "dev"], tmp_path)
    assert missing == ["package.json"] and verified == []


def test_ground_npx_grounds_on_package_json(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    verified, missing = ground_start(["npx", "next", "dev"], tmp_path)
    assert verified == ["package.json"] and missing == []


def test_ground_static_http_server_is_opaque(tmp_path: Path):
    # A stdlib `-m http.server` names no repo entrypoint — neither verified nor
    # missing; the resolver applies the static index.html fallback.
    verified, missing = ground_start(
        ["python", "-m", "http.server", "{port}", "--bind", "127.0.0.1"], tmp_path)
    assert verified == [] and missing == []


def test_ground_module_present(tmp_path: Path):
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "__init__.py").write_text("")
    verified, missing = ground_start(["python", "-m", "mypkg"], tmp_path)
    assert verified == ["mypkg/__init__.py"] and missing == []


def test_ground_module_absent(tmp_path: Path):
    verified, missing = ground_start(["python", "-m", "mypkg"], tmp_path)
    assert missing == ["mypkg.py"]


def test_ground_dunder_main(tmp_path: Path):
    (tmp_path / "__main__.py").write_text("")
    verified, missing = ground_start(["python", "-m", "."], tmp_path)
    assert verified == ["__main__.py"]


# --------------------------------------------------------------------------- #
# resolve_launch_plan — precedence + grounded-or-refuse.
# --------------------------------------------------------------------------- #
def test_grounded_stored_profile_resolves(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    rstore = _store(tmp_path)
    rstore.upsert_profile(_profile(start=["python", "app.py"], kind="api"))

    plan = resolve_launch_plan(tmp_path, "headsha", rstore, "p")

    assert isinstance(plan, LaunchPlan)
    assert plan.modality == "server"
    assert plan.grounded_by == "profile"
    assert plan.verified_paths == ["app.py"]
    assert plan.trust_tier == 0
    assert plan.head == "headsha"
    wire = plan.to_dict()
    assert wire["launch_kind"] == "server" and wire["host"] == "local sidecar"


def test_reddit_absent_start_is_rejected(tmp_path: Path):
    # Stored profile advertises `npm run dev` but there is no package.json on
    # master — the reddit-clone hallucination. It must NOT resolve.
    rstore = _store(tmp_path)
    rstore.upsert_profile(_profile(kind="web", start=["npm", "run", "dev"]))

    outcome = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(outcome, Unresolved)
    assert any("package.json" in line for line in outcome.looked_for)


def test_reddit_missing_script_is_rejected(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    rstore = _store(tmp_path)
    rstore.upsert_profile(_profile(kind="web", start=["npm", "run", "dev"]))

    outcome = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(outcome, Unresolved)
    assert any("scripts.dev" in line for line in outcome.looked_for)


def test_detector_grounds_static_when_no_profile(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html></html>")
    rstore = _store(tmp_path)

    plan = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(plan, LaunchPlan)
    assert plan.modality == "static"
    assert plan.grounded_by == "detector"
    assert plan.verified_paths == ["index.html"]


def test_detector_grounds_python_cli(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    rstore = _store(tmp_path)

    plan = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(plan, LaunchPlan)
    assert plan.modality == "cli"
    assert plan.grounded_by == "detector"
    assert "main.py" in plan.verified_paths


def test_ungrounded_stored_profile_falls_through_to_detector(tmp_path: Path):
    # A broken stored profile (npm run dev, no package.json) must not block a
    # real, detectable static site: the resolver falls through to detection.
    (tmp_path / "index.html").write_text("<html></html>")
    rstore = _store(tmp_path)
    rstore.upsert_profile(_profile(kind="web", start=["npm", "run", "dev"]))

    plan = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(plan, LaunchPlan)
    assert plan.grounded_by == "detector" and plan.modality == "static"


def test_stored_profile_takes_precedence_over_detector(tmp_path: Path):
    # Both a grounded stored profile and a detectable static exist -> the stored
    # profile wins (precedence: profile before detector).
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "index.html").write_text("<html></html>")
    rstore = _store(tmp_path)
    rstore.upsert_profile(_profile(kind="api", start=["python", "app.py"]))

    plan = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(plan, LaunchPlan) and plan.grounded_by == "profile"


def test_hallucinated_adhoc_runtime_profile_file_is_ignored(tmp_path: Path):
    # A DEV model dropped a `.runtime-profile.json` on disk advertising a Next.js
    # app for code that isn't there. The resolver reads the store + detection —
    # never ad-hoc files — so it resolves to nothing, not the fiction.
    (tmp_path / ".runtime-profile.json").write_text(
        json.dumps({"start": ["npm", "run", "dev"], "kind": "web"}))
    (tmp_path / "Navigation.tsx").write_text("export const Nav = () => null;\n")
    rstore = _store(tmp_path)

    outcome = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(outcome, Unresolved)


def test_empty_project_is_unresolved_with_checklist(tmp_path: Path):
    rstore = _store(tmp_path)

    outcome = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(outcome, Unresolved)
    assert len(outcome.looked_for) >= 1
    assert all(isinstance(line, str) for line in outcome.looked_for)


def test_unknown_kind_profile_does_not_resolve(tmp_path: Path):
    # An `unknown`-kind profile (packaging metadata, no entrypoint) has no
    # runnable modality in S1 and no start -> unresolved, with a reason.
    rstore = _store(tmp_path)
    rstore.upsert_profile(_profile(kind="unknown", start=[]))

    outcome = resolve_launch_plan(tmp_path, "h", rstore, "p")

    assert isinstance(outcome, Unresolved)


def test_grounding_holds_through_a_symlinked_workspace_path(tmp_path: Path):
    # A workspace whose path traverses a symlink (macOS `/var/folders`, a
    # symlinked `ERRORTA_HOME`/home dir, NFS) must not defeat grounding: a
    # present entrypoint stays verified, an absent one stays missing. Regression
    # for `_safe_join` comparing a resolved candidate against an unresolved base.
    real = tmp_path / "real"
    real.mkdir()
    (real / "game.py").write_text("print('hi')\n")
    link = tmp_path / "link"
    link.symlink_to(real)

    verified, missing = ground_start(["python", "game.py"], link)
    assert verified == ["game.py"]
    assert missing == []

    verified2, missing2 = ground_start(["python", "absent.py"], link)
    assert verified2 == []
    assert missing2 == ["absent.py"]


def test_symlink_escape_token_still_refused(tmp_path: Path):
    # Defense-in-depth is preserved after the resolve fix: an in-repo symlink
    # pointing outside the workspace cannot smuggle an out-of-tree entrypoint
    # into `verified` — it lands in `missing` (fail-closed), so the plan refuses.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("print('nope')\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / "escape").symlink_to(outside)

    verified, missing = ground_start(["python", "escape/evil.py"], work)
    assert "escape/evil.py" not in verified
    assert missing == ["escape/evil.py"]
