"""F101-03 — Godot game detection + grounded resolution.

A Godot project is marked by a ``project.godot`` manifest at the workspace root
and runs via the ``godot`` engine binary, which opens its own OS window — so it
classifies as a ``desktop`` runtime and grounds on ``project.godot`` (the engine
binary itself is a host-PATH dependency resolved at spawn, like cargo/go/docker).

Pure/read-only: these build a workspace dir + a ``RuntimeProfileStore`` directly
(no manager, no subprocess).
"""
from __future__ import annotations

import threading
from pathlib import Path

from errorta_council.coding.runtime import RuntimeProfileStore, detect
from errorta_council.coding.runtime_resolve import (
    LaunchPlan,
    Unresolved,
    ground_start,
    resolve_launch_plan,
)

_PROJECT_GODOT = (
    "; Engine configuration file.\n"
    'config_version=5\n\n'
    "[application]\n\n"
    'config/name="Pixel Creature RPG"\n'
    'run/main_scene="res://Main.tscn"\n'
)


def _store(tmp_path: Path) -> RuntimeProfileStore:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    return RuntimeProfileStore(ledger_dir, threading.Lock())


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
def test_detects_godot_project_as_desktop(tmp_path: Path):
    (tmp_path / "project.godot").write_text(_PROJECT_GODOT)
    props = detect(tmp_path, project_id="rpg")
    assert props and props[0].kind == "desktop"
    assert props[0].profile_id == "default"
    assert props[0].start == ["godot", "--path", "."]
    assert props[0].demo.get("toolkit") == "godot"


def test_no_project_godot_no_godot_profile(tmp_path: Path):
    (tmp_path / "Main.tscn").write_text("[gd_scene]\n")
    props = detect(tmp_path, project_id="rpg")
    assert all(p.demo.get("toolkit") != "godot" for p in props)


# --------------------------------------------------------------------------- #
# Grounding — the entrypoint-exists check.
# --------------------------------------------------------------------------- #
def test_ground_godot_manifest_present(tmp_path: Path):
    (tmp_path / "project.godot").write_text(_PROJECT_GODOT)
    verified, missing = ground_start(["godot", "--path", "."], tmp_path)
    assert verified == ["project.godot"] and missing == []


def test_ground_godot_manifest_absent(tmp_path: Path):
    verified, missing = ground_start(["godot", "--path", "."], tmp_path)
    assert verified == [] and missing == ["project.godot"]


# --------------------------------------------------------------------------- #
# End-to-end resolve — the exact path the Run button drives.
# --------------------------------------------------------------------------- #
def test_resolve_godot_project_via_detector(tmp_path: Path):
    (tmp_path / "project.godot").write_text(_PROJECT_GODOT)
    plan = resolve_launch_plan(tmp_path, "headabc", _store(tmp_path), "rpg")
    assert isinstance(plan, LaunchPlan)
    assert plan.modality == "desktop"
    assert plan.kind == "desktop"
    assert plan.start == ["godot", "--path", "."]
    assert plan.grounded_by == "detector"
    assert plan.verified_paths == ["project.godot"]


def test_resolve_unresolved_lists_godot_in_checklist(tmp_path: Path):
    # An empty workspace: the honest checklist now names Godot as a looked-for
    # project type.
    plan = resolve_launch_plan(tmp_path, "headabc", _store(tmp_path), "empty")
    assert isinstance(plan, Unresolved)
    assert any("project.godot" in line for line in plan.looked_for)
