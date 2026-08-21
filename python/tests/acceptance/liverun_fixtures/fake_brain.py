"""Fake brain for the live-run acceptance test.

    python fake_brain.py <log> <active_seconds> <ctrl_file>

Appends a ``seq=N`` line to ``<log>`` every 0.2 s for ``<active_seconds>``, then
goes SILENT but stays alive — the stall class the supervisor exists to catch: a
process that is still running and still passes ``remote_pid_alive`` while doing
nothing at all.

The log's mtime is therefore the exact instant it went quiet, which is what the
test measures the stall-detection latency against.

On SIGTERM it performs the "safe logout": writes ``LOGIN_SCREEN`` into the fake
client's control file and exits 0. That is the only thing that can make the
profile's ``logoff_verified`` check pass, so the literal cannot be forged by a
teardown step that merely exits cleanly.
"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from types import FrameType

LOG = Path(sys.argv[1])
ACTIVE_S = float(sys.argv[2])
CTRL = Path(sys.argv[3])


def _logoff(_signum: int, _frame: FrameType | None) -> None:
    CTRL.write_text("LOGIN_SCREEN")
    sys.exit(0)


signal.signal(signal.SIGTERM, _logoff)

start = time.time()
seq = 0
while True:
    if time.time() - start < ACTIVE_S:
        seq += 1
        with LOG.open("a") as fh:
            fh.write(f"seq={seq}\n")
    time.sleep(0.2)
