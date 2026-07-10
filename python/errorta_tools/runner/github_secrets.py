"""F102 — OS-keychain access for the GitHub device-flow token (D2).

The device-flow token (P3/P4) is stored in the OS keychain ONLY — never in the
ledger, diagnostics, or a config file. This module is the keychain seam. In this
slice (P2) it is READ-ONLY: ``has_token`` / ``keychain_get`` so ``auth-status``
can report whether a token is present. The write/delete paths (set on device-flow
completion, delete on revoke) land in the next slice.

``keyring`` is lazy-imported and the whole module degrades gracefully when it is
unavailable (e.g. a headless Linux box with no Secret Service): every function
returns the "no token / not available" answer rather than raising or falling
back to any plaintext store.
"""
from __future__ import annotations

_SERVICE = "Errorta-GitHub"
_USERNAME = "device-token"


def _keyring():
    """Return the ``keyring`` module, or None if it is unavailable.

    Lazy import so ``errorta_tools`` has no hard dependency on ``keyring`` and so
    an absent Secret Service backend never breaks import.
    """
    try:
        import keyring  # noqa: PLC0415 — lazy by design
    except Exception:
        return None
    return keyring


def keychain_get() -> str | None:
    """Return the stored GitHub device-flow token, or None.

    Sensitive — never log this value, never return it over HTTP. Returns None if
    ``keyring`` is unavailable or no token is stored. Never raises.
    """
    kr = _keyring()
    if kr is None:
        return None
    try:
        value = kr.get_password(_SERVICE, _USERNAME)
    except Exception:
        return None
    return value if value else None


def has_token() -> bool:
    """True iff a GitHub device-flow token is present in the OS keychain.

    Used by ``auth-status``; returns a boolean only, never the token. Degrades to
    False when ``keyring`` is unavailable. Never raises.
    """
    return keychain_get() is not None


__all__ = ["has_token", "keychain_get"]
