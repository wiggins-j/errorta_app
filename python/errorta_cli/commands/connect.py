"""``connect`` — provider configuration, writing the SAME store the app uses
(F147 §7.1, §14). Grounded against ``routes/gateway.py`` (line refs inline).

Provider set (``async_registry`` / gateway.py:34): the three HTTP-key providers
``anthropic`` / ``openai`` / ``google``; the subscription CLIs
``claude_cli`` / ``codex_cli`` / ``cursor_cli`` (addressed by the friendly names
``claudecode`` / ``codex`` / ``cursor``); ``ollama`` (the ``local`` provider); and
``custom``.

**KEY HANDLING — the load-bearing safety property (§14, golden invariant #4).** An
API key is NEVER a CLI argument — argv leaks into shell history and ``ps``. It is
read from a **no-echo prompt** (``getpass``) or ``--key-file PATH``; it travels only
in the ``PUT /provider-keys/{provider}`` JSON body (over loopback) to the store the
user chose; and it is NEVER logged or rendered. The rendered result shows only the
server-returned mask (``…<last4>`` from ``provider_keys.mask_all``). See
``test_connect_key_never_leaks``.

Write flow per §7.1:
* ``connect {anthropic|openai|google} api`` — read key (no-echo) →
  ``PUT /provider-keys/{provider}`` → ``POST /provider-keys/{provider}/test`` to
  populate the ``connected`` probe → render the mask.
* ``connect {claudecode|codex|cursor} cli`` — detect the binary
  (``GET /gateway/providers/{p}/cli-status``); optionally set the path
  (``PUT /provider-keys/{p}/cli-binary``); ``--login`` shows
  ``GET /provider-keys/{p}/login-command``; then ``POST /provider-keys/{p}/test``
  (the explicit way to populate the ``connected`` cache — gateway.py:495; a run's
  member-health preflight ALSO auto-warms it via the shared observed-connectivity
  cache, so an actively-used provider shows ``connected`` without a manual Test).
* ``connect ollama`` — show local availability (``GET /gateway/model-availability``
  / ``GET /gateway/routes?provider=local``) + the ``ERRORTA_OLLAMA_HOST`` guidance
  (the sidecar reads that env at call time; the CLI can't mutate the sidecar's env).
* ``connect custom <alias>`` — base_url + api_style + model → ``PUT /provider-keys/custom``.
* ``connect status`` (the default) — ``GET /gateway/providers`` + ``GET /provider-keys``.

Guarded writes (``PUT`` provider-keys / cli-binary) go through ``require_sole_owner``
+ the origin header (client) + the ``--yes`` gate (invariant #5). The billable
``/test`` PROBE is not a store mutation, so it needs no sole-owner guard (it only
warms the in-process ``connected`` cache — gateway.py:509).
"""
from __future__ import annotations

import getpass
from pathlib import Path
from typing import Any

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import connect as _rc
from ..session import Context
from . import _base, _mutate

# Friendly command name → provider_class.
_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "claudecode": "claude_cli",
    "claude": "claude_cli",
    "codex": "codex_cli",
    "cursor": "cursor_cli",
    "ollama": "local",
    "custom": "custom",
}
_API_PROVIDERS = ("anthropic", "openai", "google")
_CLI_PROVIDERS = ("claude_cli", "codex_cli", "cursor_cli")

# The parenthetical the confirm gate prints for a provider-key write.
_KEY_NOTE = "writes to your local provider-keys.json"


# --------------------------------------------------------------------------- #
# Key acquisition — no-echo prompt or --key-file; NEVER argv.
# --------------------------------------------------------------------------- #

def _read_key(args: dict[str, Any], *, label: str, json_mode: bool = False) -> str:
    """Acquire an API key WITHOUT it ever touching argv (§14, invariant #4).

    ``--key-file PATH`` (preferred for scripts) reads the file's first non-empty
    line; interactively we fall back to a ``getpass`` no-echo prompt. Refuses in a
    non-interactive session with no ``--key-file`` (a getpass over a pipe is unsafe
    and pointless) — and equally under ``--json``, which is contractually never
    allowed to prompt, even at a real TTY. The returned value is sensitive:
    callers must never log/render it.
    """
    key_file = args.get("key-file")
    if key_file:
        try:
            text = Path(str(key_file)).read_text(encoding="utf-8")
        except OSError:
            # Never echo the path/value: a user who mistypes their key into
            # --key-file would otherwise see it in the error text (§14).
            raise CliError(
                "could not read the file given to --key-file "
                "(check the path; the value is not shown for safety)",
                code="key_file_error",
            )
        key = text.strip().splitlines()[0].strip() if text.strip() else ""
        if not key:
            raise CliError("--key-file is empty", code="key_file_empty")
        return key
    if json_mode or not _mutate.is_interactive():
        raise CliError(
            "no key source: provide the key via --key-file PATH "
            "(a key is never passed as a CLI argument)",
            code="key_required",
        )
    key = getpass.getpass(f"{label} (input hidden): ").strip()
    if not key:
        raise CliError("no key entered", code="key_required")
    return key


# --------------------------------------------------------------------------- #
# Sub-flows. Each returns a sentinel-tagged dict for the renderer.
# --------------------------------------------------------------------------- #

def _connect_api(client: SidecarClient, ctx: Context, provider: str,
                 args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"save the {provider} API key",
                           note=_KEY_NOTE, interactive_prompt=False):
        return {"_kind": "aborted"}
    key = _read_key(args, label=f"{provider} API key", json_mode=ctx.json_mode)
    # PUT /provider-keys/{provider} (gateway.py:436) — returns mask_all().
    masked = client.put_json(f"/provider-keys/{provider}", json={"api_key": key})
    # POST /provider-keys/{provider}/test (gateway.py:477) — warms `connected`.
    test = client.post_json(f"/provider-keys/{provider}/test", json={})
    return {"_kind": "api", "provider": provider, "masked": masked, "test": test}


def _connect_cli(client: SidecarClient, ctx: Context, provider: str,
                 args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"_kind": "cli", "provider": provider}
    if args.get("binary"):
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, f"set the {provider} binary path",
                               note="writes to your local settings.json",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        # PUT /provider-keys/{provider}/cli-binary (gateway.py:258).
        out["status"] = client.put_json(
            f"/provider-keys/{provider}/cli-binary", json={"path": str(args["binary"])}
        )
    else:
        # GET /gateway/providers/{provider}/cli-status (gateway.py:242) — cheap detect.
        out["status"] = client.get_json(f"/gateway/providers/{provider}/cli-status")
    if args.get("login"):
        # GET /provider-keys/{provider}/login-command (gateway.py:296).
        out["login"] = client.get_json(f"/provider-keys/{provider}/login-command")
    # POST /provider-keys/{provider}/test (gateway.py:477) — the ONLY way to
    # populate the fail-closed `connected` cache. A billable probe, not a store
    # mutation → no sole-owner guard.
    out["test"] = client.post_json(f"/provider-keys/{provider}/test", json={})
    return out


def _connect_ollama(client: SidecarClient, ctx: Context,
                    args: dict[str, Any]) -> dict[str, Any]:
    routes = client.get_json("/gateway/routes", params={"provider": "local"})
    availability = client.get_json("/gateway/model-availability")
    return {"_kind": "ollama", "routes": routes, "availability": availability,
            "host_hint": str(args.get("host") or "")}


def _connect_custom(client: SidecarClient, ctx: Context,
                    args: dict[str, Any]) -> dict[str, Any]:
    alias = str(args.get("kind") or "").strip()
    if not alias:
        return _base.usage("connect custom <alias> --base-url URL --api-style STYLE")
    base_url = str(args.get("base-url") or "").strip()
    api_style = str(args.get("api-style") or "").strip()
    if not base_url or not api_style:
        return _base.usage(
            "connect custom <alias> --base-url URL --api-style "
            "{openai_chat_completions|anthropic_messages|raw}"
        )
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"save the custom provider '{alias}'",
                           note=_KEY_NOTE, interactive_prompt=False):
        return {"_kind": "aborted"}
    key = _read_key(args, label=f"custom '{alias}' API key", json_mode=ctx.json_mode)
    body: dict[str, Any] = {
        "alias": alias, "base_url": base_url, "api_key": key, "api_style": api_style,
    }
    if args.get("auth-header"):
        body["auth_header"] = str(args["auth-header"])
    if args.get("auth-prefix"):
        body["auth_prefix"] = str(args["auth-prefix"])
    if args.get("model"):
        body["model"] = str(args["model"])
    # PUT /provider-keys/custom (gateway.py:401) — returns mask_all().
    masked = client.put_json("/provider-keys/custom", json=body)
    test = client.post_json("/provider-keys/custom/test", params={"alias": alias}, json={})
    return {"_kind": "custom", "alias": alias, "masked": masked, "test": test}


def _connect_status(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    # Both reads — no origin guard needed, no sole-owner (invariant: reads are safe).
    providers = client.get_json("/gateway/providers")
    keys = client.get_json("/provider-keys")
    return {"_kind": "status", "providers": providers, "keys": keys}


# --------------------------------------------------------------------------- #
# Dispatch.
# --------------------------------------------------------------------------- #

def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("target") or "status").lower()
    if target in ("status", ""):
        return _connect_status(client, ctx)
    provider = _PROVIDER_ALIASES.get(target)
    if provider is None:
        return _base.usage(
            "connect {anthropic|openai|google} api | "
            "{claudecode|codex|cursor} cli | ollama | custom <alias> | status"
        )
    if provider in _API_PROVIDERS:
        return _connect_api(client, ctx, provider, args)
    if provider in _CLI_PROVIDERS:
        return _connect_cli(client, ctx, provider, args)
    if provider == "local":
        return _connect_ollama(client, ctx, args)
    if provider == "custom":
        return _connect_custom(client, ctx, args)
    return _base.usage("unknown provider")  # pragma: no cover — exhaustive above


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    from ..render import muted, render, usage_text
    usage = usage_text(payload)
    if usage is not None:
        return render(muted(f"usage: {usage}"))
    return _rc.render_connect(payload)


register(
    Command(
        name="connect",
        help="Configure AI providers (keys / CLIs / ollama / custom) + status.",
        call=_call,
        render=_render,
        params=(
            Param("target", "Provider or 'status' (default).", default="status"),
            Param("kind", "api | cli | <alias> (second token).", default=""),
            Param("key-file", "Read the API key from this file (never argv).",
                  is_flag=False),
            Param("binary", "CLI provider: set the vendor binary path.", is_flag=False),
            Param("login", "CLI provider: show the vendor login command.", is_flag=True),
            Param("host", "ollama: ERRORTA_OLLAMA_HOST guidance value.", is_flag=False),
            Param("base-url", "custom: provider base URL.", is_flag=False),
            Param("api-style", "custom: openai_chat_completions|anthropic_messages|raw.",
                  is_flag=False),
            Param("model", "custom: default model.", is_flag=False),
            Param("auth-header", "custom: auth header name.", is_flag=False),
            Param("auth-prefix", "custom: auth value prefix.", is_flag=False),
            Param("yes", "Skip the confirmation prompt (required non-interactively).",
                  is_flag=True),
        ),
    )
)
