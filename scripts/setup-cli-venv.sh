#!/usr/bin/env bash
#
# One-time setup so the Errorta CLI runs from a source checkout: create
# python/.venv and install the engine + CLI (and AIAR) editable. After this,
# `scripts/dev-errorta` works, and the real `errorta` console script exists at
# python/.venv/bin/errorta.
#
# Usage:   scripts/setup-cli-venv.sh
# Env:     PYTHON=python3.12   AIAR_SRC=/path/to/aiar
#
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
py="${PYTHON:-python3}"

command -v "$py" >/dev/null 2>&1 || { echo "setup-cli-venv: '$py' not found (set PYTHON=...)" >&2; exit 1; }

cd "$repo/python"
echo "==> Creating venv at python/.venv"
"$py" -m venv .venv
venv="$repo/python/.venv/bin/python"
"$venv" -m pip install -U pip >/dev/null

echo "==> Installing errorta (engine + CLI) editable"
"$venv" -m pip install -e '.[dev]'

# The sidecar imports `aiar`; install it editable from a local checkout.
aiar="${AIAR_SRC:-$HOME/GitHub/aiar}"
if [ -d "$aiar" ]; then
  echo "==> Installing AIAR editable from $aiar"
  "$venv" -m pip install -e "$aiar"
else
  echo "!!  AIAR source not found at '$aiar'." >&2
  echo "    Set AIAR_SRC=/path/to/aiar and re-run, or `pip install aiar-rag[rag]`," >&2
  echo "    otherwise the CLI can't boot its sidecar (it imports aiar)." >&2
fi

echo
echo "Done. Verify:"
echo "  $repo/scripts/dev-errorta --help"
echo "Put errorta on your PATH (any one of these):"
echo "  ln -sf \"$repo/scripts/dev-errorta\" /usr/local/bin/errorta"
echo "  ln -sf \"$repo/python/.venv/bin/errorta\" /usr/local/bin/errorta"
