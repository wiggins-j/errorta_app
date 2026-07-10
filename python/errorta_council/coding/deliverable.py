"""F087-20 — turn an accepted Coding Team MVP into a usable deliverable.

When the human accepts at the merge gate (MVP reached), Errorta materializes the
built project to a real, user-facing folder and tells the user where it is and
how to run it — so they can actually USE what the team built, not hunt through
Errorta's internal worktree.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _open_cmd(target: str | Path) -> str:
    """Platform-appropriate 'open this in the default app' command. Errorta ships
    on macOS, Linux, and Windows, so a delivered run hint must not assume macOS
    ``open``."""
    if sys.platform == "darwin":
        return f"open {target}"
    if sys.platform.startswith("win"):
        return f"start {target}"
    return f"xdg-open {target}"


def deliverable_dir(project_id: str, delivery_root: str | None = None) -> Path:
    """User-facing location for a delivered project.

    Precedence (F105): an explicit per-project ``delivery_root`` (the directory
    the user picked at create time) wins; then the ``ERRORTA_DELIVERABLES_DIR``
    env override; otherwise ~/Errorta Projects. The project id is always the
    final path component."""
    base = delivery_root or os.environ.get("ERRORTA_DELIVERABLES_DIR")
    root = Path(base) if base else Path.home() / "Errorta Projects"
    return root / project_id


def run_hint(dest: str | Path) -> str:
    """A best-effort 'how to run it' line, detected from the delivered files."""
    d = Path(dest)
    try:
        names = [p.name for p in d.rglob("*") if p.is_file()]
    except OSError:
        return "Open the folder to see the project."
    has = set(names)

    if "package.json" in has:
        return f"cd {d} && npm install && npm start"
    py = [n for n in names if n.endswith(".py")]
    if py:
        # a clear entry point?
        for entry in ("main.py", "app.py", "__main__.py", "cli.py"):
            if entry in has:
                return f"cd {d} && python {entry}"
        # a runnable script with an __main__ guard?
        for p in d.rglob("*.py"):
            try:
                if "__main__" in p.read_text(encoding="utf-8", errors="ignore"):
                    return f"cd {d} && python {p.name}"
            except OSError:
                continue
        if any(n.startswith("test_") or n.endswith("_test.py") for n in names):
            return f"cd {d} && python -m pytest"
        mod = py[0][:-3]
        return f'cd {d} && python -c "import {mod}; help({mod})"'
    if "index.html" in has:
        return _open_cmd(d / "index.html")
    if "Cargo.toml" in has:
        return f"cd {d} && cargo run"
    if "go.mod" in has:
        return f"cd {d} && go run ."
    return f"Open {d} to use the project."


def deliver(project_id: str, workspace: Any, *, target: str,
            repo_path: str | None,
            delivery_root: str | None = None) -> dict[str, Any]:
    """Produce the user-facing deliverable for an accepted project.

    * ``existing`` target -> the work was merged back into the user's repo; that
      repo IS the deliverable.
    * ``new`` target -> export the integrated master tree to a clean user-facing
      folder.

    Returns ``{delivered_to, open_url, run_hint}``.
    """
    if target == "existing" and repo_path:
        dest = repo_path
    else:
        dest = workspace.export(str(deliverable_dir(project_id, delivery_root)))
    return {
        "delivered_to": str(dest),
        "open_url": Path(dest).as_uri(),
        "run_hint": run_hint(dest),
    }


__all__ = ["deliverable_dir", "run_hint", "deliver"]
