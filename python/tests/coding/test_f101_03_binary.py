"""F101-03 S4 — native binaries + build-manifest runtimes.

Detector reads the Mach-O/ELF header for os/arch; BinaryLauncher refuses a
foreign host and runs a matching one. Live-validated on this host: a real
clang-compiled binary is detected, grounded, and run through the dispatch;
a synthesized foreign-arch header is refused with a reason.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import (
    RuntimeProfileStore,
    binary_host_requirements,
    current_host_platform,
    detect,
)
from errorta_council.coding.runtime_launchers import BinaryLauncher, get_launcher
from errorta_council.coding.runtime_process import RuntimeProcessError, RuntimeProcessManager
from errorta_council.coding.runtime_resolve import ground_start, resolve_launch_plan
from errorta_council.coding.workspace import CodingWorkspace


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    yield
    rp.teardown_all()


def _write_elf(path: Path, *, machine: int, ei_data: int = 1) -> None:
    head = bytearray(20)
    head[0:4] = b"\x7fELF"
    head[4] = 2          # 64-bit
    head[5] = ei_data    # 1 = little-endian
    endian = "<" if ei_data == 1 else ">"
    struct.pack_into(endian + "H", head, 18, machine)
    path.write_bytes(bytes(head) + b"\x00" * 44)
    path.chmod(0o755)


# --------------------------------------------------------------------------- #
# Header parsing
# --------------------------------------------------------------------------- #
def test_parses_elf_x86_64(tmp_path: Path):
    b = tmp_path / "app"
    _write_elf(b, machine=0x3E)
    assert binary_host_requirements(b) == {"os": "linux", "arch": "x86_64"}


def test_parses_elf_aarch64(tmp_path: Path):
    b = tmp_path / "app"
    _write_elf(b, machine=0xB7)
    assert binary_host_requirements(b) == {"os": "linux", "arch": "arm64"}


def test_non_binary_returns_none(tmp_path: Path):
    b = tmp_path / "notes.txt"
    b.write_text("just text\n")
    assert binary_host_requirements(b) is None


def test_truncated_elf_refuses_without_crashing(tmp_path: Path):
    # A +x file with the ELF magic but a header too short to hold e_machine
    # (bytes 18:20) must NOT crash the header sniff (would 500 the detect/run
    # route); it's refused as "not a recognized binary" instead.
    b = tmp_path / "app"
    b.write_bytes(b"\x7fELF\x01\x01\x00\x00\x00\x00\x00\x00")  # 12 bytes
    b.chmod(0o755)
    assert binary_host_requirements(b) is None
    # And it must not blow up the whole detector sweep either.
    assert detect(tmp_path, project_id="trunc") == []


@pytest.mark.skipif(shutil.which("clang") is None or __import__("sys").platform != "darwin",
                    reason="needs clang on macOS to build a real Mach-O")
def test_parses_real_macho(tmp_path: Path):
    src = tmp_path / "m.c"
    src.write_text("int main(){return 0;}\n")
    out = tmp_path / "prog"
    subprocess.run(["clang", str(src), "-o", str(out)], check=True, capture_output=True)
    req = binary_host_requirements(out)
    assert req is not None and req["os"] == "macos"
    assert req["arch"] == current_host_platform()["arch"]


# --------------------------------------------------------------------------- #
# Detection + grounding
# --------------------------------------------------------------------------- #
def test_detects_native_binary(tmp_path: Path):
    _write_elf(tmp_path / "main", machine=0x3E)
    props = detect(tmp_path, project_id="b")
    assert props and props[0].kind == "binary"
    assert props[0].start == ["./main"]
    assert props[0].to_dict()["host_requirements"] == {"os": "linux", "arch": "x86_64"}


def test_detects_cargo_build_manifest(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    props = detect(tmp_path, project_id="r")
    assert props[0].kind == "cli" and props[0].start == ["cargo", "run"]


def test_grounds_build_tool_on_manifest(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n")
    verified, missing = ground_start(["go", "run", "."], tmp_path)
    assert verified == ["go.mod"] and missing == []


def test_build_tool_without_manifest_is_ungrounded(tmp_path: Path):
    verified, missing = ground_start(["cargo", "run"], tmp_path)
    assert missing == ["Cargo.toml"]


# --------------------------------------------------------------------------- #
# BinaryLauncher host gate
# --------------------------------------------------------------------------- #
def test_binary_launcher_refuses_foreign_host(tmp_path: Path):
    from errorta_council.coding.runtime_resolve import LaunchPlan
    host = current_host_platform()
    foreign_os = "windows" if host["os"] != "windows" else "linux"
    plan = LaunchPlan(
        modality="binary", profile_id="default", kind="binary",
        start=["./app"], setup=[], working_dir=".", ports=[], health={},
        env_required=[], grounded_by="detector", verified_paths=["./app"],
        host_requirements={"os": foreign_os, "arch": "x86_64"})

    with pytest.raises(RuntimeProcessError) as exc:
        BinaryLauncher().launch(_FakeMgr(), plan)
    assert "binary_host_mismatch" in str(exc.value)


class _FakeMgr:
    def run_cli(self, profile_id):  # pragma: no cover - should not be reached
        raise AssertionError("must not run a foreign-host binary")


# --------------------------------------------------------------------------- #
# Live: a real compiled binary runs through the dispatch on this host.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("clang") is None,
                    reason="needs clang to build a native binary")
def test_live_native_binary_runs_through_dispatch(tmp_errorta_home: Path):
    store = LedgerStore("binrun")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace("binrun", store)
    ws.setup(target="new", repo_path=None)
    root = ws.root()
    (root / "m.c").write_text('#include <stdio.h>\nint main(){printf("ok\\n");return 0;}\n')
    subprocess.run(["clang", str(root / "m.c"), "-o", str(root / "app")],
                   check=True, capture_output=True)
    (root / "m.c").unlink()  # leave only the binary

    mgr = RuntimeProcessManager.for_project("binrun")
    rstore = RuntimeProfileStore.for_ledger(store)
    plan = resolve_launch_plan(root, "h", rstore, "binrun")
    assert plan.modality == "binary"
    assert plan.host_requirements["os"] == current_host_platform()["os"]

    rstore.upsert_profile(plan.source_profile)
    session = get_launcher("binary").launch(mgr, plan)
    sid = session.session_id

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        s = mgr.get_session(sid)
        if s and s.state in ("stopped", "crashed"):
            break
        time.sleep(0.05)
    s = mgr.get_session(sid)
    assert s is not None and s.state == "stopped" and s.exit_code == 0
