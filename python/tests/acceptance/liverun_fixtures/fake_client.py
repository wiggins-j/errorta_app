"""Fake game client for the live-run acceptance test.

    python fake_client.py <port> <ctrl_file> <pidfile>

Serves ``GET /state`` -> ``{"gameState": <contents of CTRL or LOGGED_IN>}`` on
``127.0.0.1:<port>``.

It DAEMONISES (bind, then double-fork, then redirect stdio to /dev/null) so the
process the profile's ``local`` launch step spawns exits 0 immediately, exactly
like the real ``osascript``/``jagex-play`` launcher the OSRS profile uses. That
matters twice over: `errorta_liverun.steps._run_local` waits on
``Popen.communicate``, so a launcher that stayed in the foreground would hang
until the step timeout, and it reads the child's stdout/stderr pipes to EOF, so
the daemon has to let go of them.

The pidfile is how the profile's teardown reaches it: the server survives its
launcher, so a ``remote_signal`` teardown step (through the acceptance test's
fake ``ssh``, which runs the remote argv locally) is what stops it.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
from pathlib import Path

PORT = int(sys.argv[1])
CTRL = Path(sys.argv[2])
PIDFILE = Path(sys.argv[3])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        state = CTRL.read_text().strip() if CTRL.exists() else "LOGGED_IN"
        body = json.dumps({"gameState": state}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence the stderr access log
        return


def _detach() -> None:
    """Classic double fork. The first child leads a new session (so the server
    has no controlling terminal); the grandchild is what actually serves."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if devnull > 2:
        os.close(devnull)


def main() -> None:
    # Bind BEFORE detaching: a port clash has to be reported on stderr and show
    # up as a non-zero exit of the launch step, not as a silent daemon that
    # never answers the step's http check.
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    _detach()
    PIDFILE.write_text(str(os.getpid()))
    server.serve_forever()


main()
