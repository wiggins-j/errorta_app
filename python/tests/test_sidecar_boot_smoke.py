"""Hermetic smoke test for the sidecar boot sequence under uvicorn.

Closes the gap between the in-process `TestClient` walkthrough in
`test_demo_walkthrough_phase5.py` and the demo-day reality of
running `python -m errorta_app.server` under uvicorn next to a Vite
or Tauri dev process. Spawns the sidecar via `subprocess.Popen` on an
ephemeral port, verifies `/healthz` and `/council/rooms`, and tears
down cleanly via terminate -> wait -> kill cascade.

Skips loudly (does NOT error) when `uvicorn` is not installed: that
is the documented case for the dev machine.

See: docs/specs/F031-DEMO-BOOT-VERIFY-boot-sequence.md
     docs/superpowers/plans/2026-06-12-F031-DEMO-BOOT-VERIFY.md
"""
from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
from typing import Iterator

import httpx
import pytest

try:
    import uvicorn  # noqa: F401

    _UVICORN_AVAILABLE = True
except ImportError:
    _UVICORN_AVAILABLE = False
    pytestmark = pytest.mark.skip(
        reason="uvicorn not installed; run: pip install uvicorn"
    )


def _free_port() -> int:
    """Return an ephemeral TCP port on 127.0.0.1.

    Bind to port 0, read the kernel-assigned port, and close the socket
    before returning. Reproduces the F-INFRA-12 residency-smoke pattern.

    There is a small TOCTOU race window between releasing the port here
    and uvicorn binding to it in the spawned process; the smoke test
    accepts this — collision on an ephemeral 5-digit port across two
    consecutive operations is vanishingly rare in practice.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def _sidecar_proc(tmp_path) -> Iterator[tuple[subprocess.Popen, int, str]]:
    """Spawn `python -m errorta_app.server` on an ephemeral port.

    Yields (proc, port, stderr_path). Tears down via the
    terminate -> wait(5) -> kill cascade so a hung child is still
    reaped before pytest moves on. `start_new_session=True` so the
    spawned uvicorn lives in its own process group: SIGINT delivered
    to the test runner does not double-fire on the child mid-teardown.

    QA P1 #3 (2026-06-12): pin ``ERRORTA_HOME`` to a tmp_path-rooted
    directory so the spawned sidecar's boot-recovery sweep (which
    scans every run dir under runs_dir and rewrites mid-flight state)
    cannot touch real operator state on a developer machine. Same
    reason we keep stdout/err captured to a temp file.
    """
    port = _free_port()
    stderr = tempfile.NamedTemporaryFile(
        prefix="errorta-sidecar-smoke-", suffix=".log", delete=False
    )
    stderr_path = stderr.name
    stderr.close()
    # QA P1 #3: hermetic ERRORTA_HOME. The sidecar's startup hook calls
    # _scan_and_recover() against runs_dir() which is rooted at
    # ERRORTA_HOME. Without isolation, the smoke test would walk the
    # operator's real council runs and mark mid-flight ones as
    # `interrupted`.
    isolated_home = tmp_path / "errorta-home"
    isolated_home.mkdir()
    env = {
        **os.environ,
        "ERRORTA_SIDECAR_PORT": str(port),
        "ERRORTA_HOME": str(isolated_home),
    }
    # Quieter uvicorn logs in the smoke test — we surface stderr only on
    # failure anyway. Honor ERRORTA_LOG_LEVEL if the operator set one.
    env.setdefault("ERRORTA_LOG_LEVEL", "warning")
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "errorta_app.server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "wb"),  # noqa: SIM115 — Popen owns the fd
        start_new_session=True,
    )
    try:
        yield proc, port, stderr_path
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _tail_stderr(path: str, lines: int = 40) -> str:
    """Return the last `lines` lines of the captured stderr file."""
    try:
        text = pathlib.Path(path).read_text(errors="replace")
    except OSError as exc:  # pragma: no cover — defensive
        return f"<could not read stderr at {path}: {exc}>"
    tail = text.splitlines()[-lines:]
    return "\n".join(tail)


def _poll_healthz(
    port: int, stderr_path: str, *, budget_s: float = 15.0
) -> dict:
    """Poll GET /healthz on 127.0.0.1:port until it returns 200.

    On timeout, raise AssertionError carrying the tail of the captured
    sidecar stderr so CI logs explain what happened.
    """
    deadline = time.monotonic() + budget_s
    last_err: Exception | None = None
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                r = client.get(f"http://127.0.0.1:{port}/healthz")
                if r.status_code == 200:
                    return r.json()
            except httpx.HTTPError as exc:
                last_err = exc
            time.sleep(0.05)
    raise AssertionError(
        f"healthz on 127.0.0.1:{port} did not become ready within "
        f"{budget_s}s (last error: {last_err!r}).\n"
        f"--- sidecar stderr (last 40 lines) ---\n"
        f"{_tail_stderr(stderr_path)}"
    )


def test_sidecar_boots_and_healthz_reports_ready(
    _sidecar_proc: tuple[subprocess.Popen, int, str],
) -> None:
    """`python -m errorta_app.server` boots under uvicorn and /healthz
    returns the v015-prep `aiar_pin` contract."""
    _proc, port, stderr_path = _sidecar_proc
    body = _poll_healthz(port, stderr_path)
    assert isinstance(body, dict), "healthz body should be a JSON object"
    assert "aiar_pin" in body, f"healthz missing aiar_pin block; got: {body!r}"
    pin = body["aiar_pin"]
    assert isinstance(pin, dict), f"aiar_pin should be an object; got: {pin!r}"
    assert isinstance(pin.get("available"), bool), (
        f"aiar_pin.available must be bool; got: {pin.get('available')!r}"
    )
    assert isinstance(pin.get("source"), str), (
        f"aiar_pin.source must be str; got: {pin.get('source')!r}"
    )


def test_council_rooms_empty_list_on_fresh_boot(
    _sidecar_proc: tuple[subprocess.Popen, int, str],
) -> None:
    """Council routes mount under real uvicorn (not just TestClient).

    QA P1 #3 (2026-06-12): fixed the response-shape assertion. The
    route at errorta_app/routes/council.py:99 returns
    ``{"rooms": [...]}`` — NOT a bare list. The previous assertion
    ``isinstance(body, list)`` was a latent bug that would have
    failed the smoke whenever uvicorn was actually installed.

    With the tmp_path-isolated ERRORTA_HOME from the fixture, this
    boot is guaranteed clean — zero rooms.
    """
    _proc, port, stderr_path = _sidecar_proc
    # Wait for healthz first so we know the app has finished startup.
    _poll_healthz(port, stderr_path)
    with httpx.Client(timeout=2.0) as client:
        r = client.get(f"http://127.0.0.1:{port}/council/rooms")
    assert r.status_code == 200, (
        f"GET /council/rooms returned {r.status_code}; body: {r.text[:200]!r}"
    )
    body = r.json()
    assert isinstance(body, dict), (
        f"/council/rooms should return a JSON object; got: {type(body).__name__}"
    )
    assert "rooms" in body, (
        f"/council/rooms response missing 'rooms' key; got keys: {sorted(body)}"
    )
    rooms = body["rooms"]
    assert isinstance(rooms, list), (
        f"body['rooms'] should be a list; got: {type(rooms).__name__}"
    )
    # ERRORTA_HOME is tmp_path-rooted in this fixture — fresh boot, no rooms.
    assert rooms == [], (
        f"expected empty rooms on hermetic boot; got: {rooms!r}"
    )


def test_skip_when_uvicorn_missing() -> None:
    """Meta-test: the uvicorn-missing skip wiring at module load.

    The skip is wired at module import time via `pytestmark`, so by the
    time this test body runs the import already succeeded (otherwise the
    whole module is skipped and no test bodies run). We can't toggle
    `uvicorn` availability after the fact; instead we assert that the
    module source carries the `try / except ImportError` block AND
    sets `pytestmark` in the except branch. Source-parse form is the
    safest of the three options the plan allows (importlib re-import
    with masked sys.modules is brittle around editable installs).
    """
    src = pathlib.Path(__file__).read_text()
    assert "import uvicorn" in src, (
        "uvicorn import statement missing — skip wiring broken"
    )
    assert "except ImportError" in src, (
        "ImportError-guarded import missing — skip wiring broken"
    )
    assert "pytestmark = pytest.mark.skip" in src, (
        "pytestmark skip declaration missing — skip wiring broken"
    )
    assert "pip install uvicorn" in src, (
        "install-pointer string missing from skip reason"
    )
    # And the actual module-load result is consistent with the source: if
    # uvicorn is importable, _UVICORN_AVAILABLE must be True; otherwise
    # the whole module would be skipped and this body would not execute.
    assert _UVICORN_AVAILABLE is True, (
        "test body ran but _UVICORN_AVAILABLE is False — "
        "skip wiring did not gate the module"
    )
