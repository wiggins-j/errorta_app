"""F010 export copy slice: progress, dry-run, integrity, idempotency, resume."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from errorta_export import (
    CopyResult,
    ExportIntegrityError,
    copy_with_progress,
)
from errorta_export.planner import ExportFile, ExportPlan


def _make_file(path: Path, payload: bytes) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return len(payload), hashlib.sha256(payload).hexdigest()


def _plan_with(files: list[ExportFile]) -> ExportPlan:
    plan = ExportPlan(files=files)
    plan.total_size_bytes = sum(f.size_bytes for f in files)
    return plan


def _deterministic_payload(seed: int, size: int) -> bytes:
    # Simple deterministic pattern; not random, but reproducible and varied.
    base = bytes((seed + i) & 0xFF for i in range(min(size, 256)))
    if size <= 256:
        return base[:size]
    # Tile it out
    full, rem = divmod(size, 256)
    return base * full + base[:rem]


def test_progress_callback_monotonic_and_final(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    # Use a payload larger than chunk_size so multiple chunks fire.
    chunk_size = 1024
    payload1 = _deterministic_payload(1, 3500)  # 4 chunks
    payload2 = _deterministic_payload(2, 700)  # 1 chunk

    f1 = src_dir / "a.bin"
    f2 = src_dir / "b.bin"
    s1, h1 = _make_file(f1, payload1)
    s2, h2 = _make_file(f2, payload2)

    plan = _plan_with(
        [
            ExportFile(src_path=f1, dest_path=dest_dir / "a.bin", size_bytes=s1, sha256_hex=h1),
            ExportFile(src_path=f2, dest_path=dest_dir / "b.bin", size_bytes=s2, sha256_hex=h2),
        ]
    )

    calls: list[tuple[int, int, int]] = []

    def cb(idx: int, done: int, total: int) -> None:
        calls.append((idx, done, total))

    result = copy_with_progress(plan, progress_cb=cb, chunk_size=chunk_size)

    assert result.files_copied == 2
    assert result.files_skipped == 0
    assert result.bytes_written == s1 + s2

    # Per-file: at least one call; bytes_done monotonically non-decreasing; final == size.
    by_file: dict[int, list[tuple[int, int]]] = {}
    for idx, done, total in calls:
        by_file.setdefault(idx, []).append((done, total))

    assert set(by_file.keys()) == {0, 1}
    for idx, entries in by_file.items():
        size = [s1, s2][idx]
        # Monotonic non-decreasing
        dones = [d for d, _ in entries]
        assert dones == sorted(dones)
        # Final equals size_bytes
        assert entries[-1][0] == size
        assert entries[-1][1] == size
        # All totals match size
        for _, t in entries:
            assert t == size

    # Multi-chunk file should have more than one progress event.
    assert len(by_file[0]) >= 2


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    dest_dir = tmp_path / "target"

    payload1 = _deterministic_payload(3, 500)
    payload2 = _deterministic_payload(4, 1200)

    f1 = src_dir / "x.bin"
    f2 = src_dir / "nested" / "y.bin"
    s1, h1 = _make_file(f1, payload1)
    s2, h2 = _make_file(f2, payload2)

    plan = _plan_with(
        [
            ExportFile(src_path=f1, dest_path=dest_dir / "x.bin", size_bytes=s1, sha256_hex=h1),
            ExportFile(
                src_path=f2,
                dest_path=dest_dir / "nested" / "y.bin",
                size_bytes=s2,
                sha256_hex=h2,
            ),
        ]
    )

    result = copy_with_progress(plan, dry_run=True)

    assert not dest_dir.exists(), "target_dir tree must remain absent on dry-run"
    assert result.bytes_written == 0
    assert result.bytes_would_write == s1 + s2
    assert result.files_copied == 0
    assert result.files_skipped == 0


def test_integrity_mismatch_raises_and_cleans_partial(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"

    payload = _deterministic_payload(5, 2048)
    f = src_dir / "bad.bin"
    size, _real_hash = _make_file(f, payload)

    # Plant a wrong expected sha so the post-copy comparison fails.
    bogus_sha = "0" * 64

    dest = dest_dir / "bad.bin"
    plan = _plan_with(
        [ExportFile(src_path=f, dest_path=dest, size_bytes=size, sha256_hex=bogus_sha)]
    )

    with pytest.raises(ExportIntegrityError) as excinfo:
        copy_with_progress(plan, chunk_size=512)

    err = excinfo.value
    assert err.dest_path == dest
    assert err.expected_sha == bogus_sha
    assert err.actual_sha == hashlib.sha256(payload).hexdigest()

    # Neither the final dest nor the .partial sidecar should remain.
    assert not dest.exists()
    partial = dest.with_suffix(dest.suffix + ".partial")
    assert not partial.exists()


def test_idempotent_skip_when_dest_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"

    payload = _deterministic_payload(6, 1500)
    f = src_dir / "same.bin"
    size, sha = _make_file(f, payload)

    dest = dest_dir / "same.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)  # pre-populate matching dest

    plan = _plan_with(
        [ExportFile(src_path=f, dest_path=dest, size_bytes=size, sha256_hex=sha)]
    )

    # Spy on builtins.open: src must NOT be opened for write-path streaming.
    import builtins as _b

    real_open = _b.open
    opens: list[tuple[str, str]] = []

    def spy_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            opens.append((str(file), str(mode)))
        except Exception:
            pass
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(_b, "open", spy_open)

    calls: list[tuple[int, int, int]] = []

    def cb(idx: int, done: int, total: int) -> None:
        calls.append((idx, done, total))

    result = copy_with_progress(plan, progress_cb=cb, chunk_size=512)

    assert result.files_skipped == 1
    assert result.files_copied == 0
    assert result.bytes_written == 0
    # Progress event fired at least once with final == size.
    assert calls
    assert calls[-1] == (0, size, size)

    # Confirm src was never opened in binary-write or binary-read-for-copy mode against
    # the source path (it should only be hashed via dest_path, not re-streamed from src).
    src_opens_rb = [o for o in opens if o[0] == str(f) and "b" in o[1] and "r" in o[1]]
    assert src_opens_rb == [], f"src should not be re-read for writing; got {src_opens_rb}"


def test_resume_after_simulated_interrupt(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"

    payload1 = _deterministic_payload(7, 1024)
    payload2 = _deterministic_payload(8, 2048)

    f1 = src_dir / "one.bin"
    f2 = src_dir / "two.bin"
    s1, h1 = _make_file(f1, payload1)
    s2, h2 = _make_file(f2, payload2)

    plan = _plan_with(
        [
            ExportFile(src_path=f1, dest_path=dest_dir / "one.bin", size_bytes=s1, sha256_hex=h1),
            ExportFile(src_path=f2, dest_path=dest_dir / "two.bin", size_bytes=s2, sha256_hex=h2),
        ]
    )

    # Interrupt: raise on the second file's first progress callback.
    class _Interrupt(RuntimeError):
        pass

    state = {"raised": False}

    def cb_interrupt(idx: int, done: int, total: int) -> None:
        if idx == 1 and not state["raised"]:
            state["raised"] = True
            raise _Interrupt("simulated mid-copy interrupt")

    with pytest.raises(_Interrupt):
        copy_with_progress(plan, progress_cb=cb_interrupt, chunk_size=256)

    # First file should be fully intact and verified.
    dest1 = dest_dir / "one.bin"
    dest2 = dest_dir / "two.bin"
    assert dest1.exists()
    assert hashlib.sha256(dest1.read_bytes()).hexdigest() == h1
    # Second file should not exist as a final file. A .partial may or may not be present;
    # the rerun must overwrite/skip it cleanly.
    assert not dest2.exists()

    # Clean up any leftover partial sidecar for f2 (acceptable for an interrupted run).
    partial2 = dest2.with_suffix(dest2.suffix + ".partial")
    if partial2.exists():
        partial2.unlink()

    # Rerun without the interrupt — first file should be skipped, second copied.
    result = copy_with_progress(plan, chunk_size=256)

    assert result.files_skipped == 1
    assert result.files_copied == 1
    assert result.files_skipped + result.files_copied == len(plan.files)
    assert dest2.exists()
    assert hashlib.sha256(dest2.read_bytes()).hexdigest() == h2
