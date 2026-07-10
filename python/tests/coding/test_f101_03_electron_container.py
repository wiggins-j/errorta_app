"""F101-03 S6 — Electron desktop detection + container through the launcher seam."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from errorta_council.coding.runtime import RuntimeProfileStore, detect
from errorta_council.coding.runtime_launchers import ContainerLauncher, get_launcher
from errorta_council.coding.runtime_resolve import (
    LaunchPlan,
    ground_start,
    resolve_launch_plan,
)


def _store(tmp_path: Path) -> RuntimeProfileStore:
    d = tmp_path / "ledger"
    d.mkdir()
    return RuntimeProfileStore(d, threading.Lock())


def test_detects_electron_as_desktop(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({
        "main": "main.js",
        "dependencies": {"electron": "^30"},
        "scripts": {"start": "electron ."},
    }))
    (tmp_path / "main.js").write_text("// electron main\n")
    props = detect(tmp_path, project_id="e")
    assert props[0].kind == "desktop"
    assert props[0].start == ["npm", "start"]
    assert props[0].demo.get("toolkit") == "electron"


def test_electron_resolves_to_desktop_t1(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({
        "main": "main.js", "dependencies": {"electron": "^30"},
        "scripts": {"start": "electron ."},
    }))
    (tmp_path / "main.js").write_text("// main\n")
    plan = resolve_launch_plan(tmp_path, "h", _store(tmp_path), "e")
    assert isinstance(plan, LaunchPlan)
    assert plan.modality == "desktop" and plan.trust_tier == 1


def test_container_launcher_registered():
    launcher = get_launcher("container")
    assert isinstance(launcher, ContainerLauncher) and launcher.modality == "container"


def test_grounds_docker_compose_on_compose_file(tmp_path: Path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    verified, missing = ground_start(["docker", "compose", "up", "--build"], tmp_path)
    assert verified == ["compose.yaml"] and missing == []


def test_grounds_docker_run_on_dockerfile(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    verified, missing = ground_start(
        ["docker", "run", "--rm", "img"], tmp_path)
    assert verified == ["Dockerfile"] and missing == []


def test_docker_without_manifest_is_ungrounded(tmp_path: Path):
    verified, missing = ground_start(["docker", "compose", "up"], tmp_path)
    assert missing == ["a compose file"]


def test_container_profile_resolves_via_seam(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    plan = resolve_launch_plan(tmp_path, "h", _store(tmp_path), "c")
    assert isinstance(plan, LaunchPlan)
    assert plan.modality == "container" and plan.grounded_by == "detector"
