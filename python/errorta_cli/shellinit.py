"""F149 — shell integration for ``errorta`` (the auto-``cd`` hook).

``errorta shell-init <zsh|bash>`` prints a wrapper function the user evals from
their rc file:

    eval "$(errorta shell-init zsh)"     # ~/.zshrc
    eval "$(errorta shell-init bash)"    # ~/.bashrc

The wrapper runs the real binary with ``ERRORTA_CD_FILE`` pointed at a temp file;
if the binary writes a directory there (it does after ``errorta new`` — see
commands.project.emit_cd_target; ``open``/``switch``/``import`` are a documented
follow-up), the wrapper ``cd``s the *parent shell* into it. A binary cannot
change its parent's working directory itself, which is why this must be a shell
function.

Nothing here spawns a sidecar — it is pure text output, safe to run at every
shell startup.
"""
from __future__ import annotations

import os

from .errors import CliError

_SUPPORTED = ("zsh", "bash")


def detect_shell() -> tuple[str, str]:
    """Best-effort ``(shell, rc_file)`` from ``$SHELL`` for the auto-cd tip.

    Only zsh and bash are supported by the hook; anything else (or an unset
    ``$SHELL``) falls back to zsh + ``~/.zshrc`` (the macOS default login shell).
    """
    shell = (os.environ.get("SHELL") or "").rsplit("/", 1)[-1]
    if shell == "bash":
        return "bash", "~/.bashrc"
    return "zsh", "~/.zshrc"

# One POSIX-ish function body serves both zsh and bash: `local`, `builtin cd`,
# and `command <tool>` all work in each. Quoting is careful because the default
# projects path (~/Errorta Projects) contains a space.
_HOOK = r'''# errorta shell integration ({shell}) — auto-cd into new projects.
# Added by: eval "$(errorta shell-init {shell})"
errorta() {{
  local __errorta_cd __errorta_rc __errorta_dir
  __errorta_cd="$(command mktemp -t errorta-cd.XXXXXX 2>/dev/null)" || __errorta_cd=""
  if [ -z "$__errorta_cd" ]; then
    command errorta "$@"
    return $?
  fi
  ERRORTA_CD_FILE="$__errorta_cd" command errorta "$@"
  __errorta_rc=$?
  if [ -s "$__errorta_cd" ]; then
    __errorta_dir="$(command cat "$__errorta_cd")"
    if [ -n "$__errorta_dir" ] && [ -d "$__errorta_dir" ]; then
      builtin cd -- "$__errorta_dir"
    fi
  fi
  command rm -f "$__errorta_cd"
  return $__errorta_rc
}}
'''


def render_hook(shell: str) -> str:
    """Return the shell function text for ``shell`` (``zsh`` or ``bash``)."""
    s = (shell or "").strip().lower()
    if s not in _SUPPORTED:
        raise CliError(
            f"unsupported shell '{shell}'. Supported: {', '.join(_SUPPORTED)}.")
    return _HOOK.format(shell=s)
