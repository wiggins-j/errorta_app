"""``python -m errorta_cli`` → the Typer app (and the ``__serve__`` re-exec)."""
from __future__ import annotations

from .app import main

if __name__ == "__main__":
    main()
