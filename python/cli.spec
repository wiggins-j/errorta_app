# PyInstaller spec for the Errorta `errorta` multicall binary (F147 §11).
#
# Build with:
#   pyinstaller cli.spec
#
# Output: dist/errorta (single executable)
#
# This is the SELF-CONTAINED headless CLI. One binary is BOTH the CLI and the
# embedded sidecar: `errorta ...` is the argv / slash-REPL front-end, and the
# hidden `errorta __serve__` subcommand runs the uvicorn sidecar in-process. The
# CLI spawns its own sidecar by re-executing THIS binary with `__serve__`
# (errorta_cli/sidecar.py:_serve_argv — `sys.executable` is the frozen binary,
# so there is no separate `python` to shell out to). Because `__serve__` imports
# errorta_app.server (the whole engine), this spec MUST bundle exactly what
# sidecar.spec bundles — the same AIAR-editable finder resolution + the same
# hiddenimports — PLUS the CLI front-end deps (typer / rich / prompt_toolkit) and
# the dynamically-imported command modules.
#
# Building is a maintainer / CI step from a fully-configured venv (AIAR editable,
# pyinstaller installed); it is NOT run in the dev/test loop. Keep this file in
# lockstep with sidecar.spec: any hiddenimport added there for the sidecar must
# be mirrored here, or the frozen `errorta __serve__` regresses vs the desktop
# sidecar.

# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# AIAR is installed editable (PEP 660 via setuptools) using a meta-path
# finder; PyInstaller's static Analyzer can't follow the custom finder so
# we resolve the AIAR source root from the installed finder file and add
# its parent to pathex so the `aiar` package gets included directly. The
# finder lives at .venv/lib/pythonX.Y/site-packages/__editable___aiar_*_finder.py
# and exposes a MAPPING dict pointing at the source dir.
#
# NOTE: this is the SAME resolver sidecar.spec uses (F147 §11 / golden invariant
# #6: reuse the sidecar's AIAR bundling, don't reinvent it). Kept byte-identical
# so the two frozen binaries carry the same `aiar` tree.
def _aiar_source_path() -> str:
    import glob as _glob
    import importlib.util as _iu
    import os.path as _osp
    # Walk every site-packages on sys.path looking for the finder module. The
    # finder file name encodes the distribution + version, which has changed
    # over time (`aiar` 0.1.0 -> `aiar-rag` 0.2.x per the AIAR pin), and a
    # stale finder for a removed source tree can linger alongside the live one
    # (pip -e doesn't always clean the old finder). So glob ALL aiar finders and
    # pick the first whose `aiar` source dir actually exists on disk rather than
    # hardcoding one version-specific name (which silently bundles nothing when
    # it points at a deleted checkout -> aiar_pin.source="absent").
    for entry in __import__("sys").path:
        for candidate in sorted(_glob.glob(
            _osp.join(entry, "__editable___aiar*_finder.py")
        )):
            spec = _iu.spec_from_file_location(
                "__aiar_finder_probe__", candidate
            )
            if spec is None or spec.loader is None:
                continue
            mod = _iu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mapping = getattr(mod, "MAPPING", {})
            aiar_dir = mapping.get("aiar")
            if aiar_dir and _osp.isdir(aiar_dir):
                # pathex wants the PARENT of the aiar/ package directory.
                return _osp.dirname(aiar_dir.rstrip("/\\"))
    return ""


_AIAR_PATHEX = _aiar_source_path()

# Build provenance: scripts/build-sidecar.sh (or the CLI build script) writes
# errorta_app/_build_info.json from the git HEAD right before this spec runs, so
# the frozen `errorta __serve__` sidecar reports its commit on /healthz (the
# CLI's stale-build / commit-mismatch check reads it). Bundle it only if present.
_build_info_datas = (
    [("errorta_app/_build_info.json", "errorta_app")]
    if os.path.isfile("errorta_app/_build_info.json") else []
)

# The command modules are imported for their registration side effects via a
# dynamic `importlib.import_module(f".commands.{name}")` loop
# (errorta_cli/registry.py), which PyInstaller's static analyzer can't follow —
# so declare each one (and the render package they pull in) explicitly, or the
# frozen CLI would register ZERO commands.
_CLI_COMMAND_MODULES = [
    f"errorta_cli.commands.{_name}"
    for _name in (
        "status", "log", "decisions", "tasks", "prs", "tokens", "turns",
        "attention", "runtime", "team", "models", "governance", "pm",
        "runctl", "connect", "wizard", "project", "focus",
        "interject", "task", "files", "publish", "grounding", "testcfg",
    )
]

a = Analysis(
    ["cli_main.py"],
    pathex=["."] + ([_AIAR_PATHEX] if _AIAR_PATHEX else []),
    binaries=[],
    datas=[
        ("errorta_hwdetect/recommendations.json", "errorta_hwdetect"),
        ("errorta_ollama/known_hashes.json", "errorta_ollama"),
        ("errorta_welcome/pinned_hash.json", "errorta_welcome"),
        # F145: the PM Reference Document the conversational PM reads at runtime.
        # Resolved via sys._MEIPASS/docs/coding in pm_reference.py.
        ("../docs/coding/PM_REFERENCE.md", "docs/coding"),
    ] + _build_info_datas,
    hiddenimports=[
        # ---- CLI front-end deps ------------------------------------------ #
        # typer / rich / click are statically imported from errorta_cli.app so
        # the analyzer catches them; prompt_toolkit is lazy-imported inside
        # errorta_cli.repl.run_repl (function body), so the static scanner misses
        # it and the frozen REPL would crash on launch. Declare it + the two
        # submodules run_repl uses.
        "prompt_toolkit",
        "prompt_toolkit.completion",
        "prompt_toolkit.history",
        # The CLI package + its dynamically-imported command modules (see above).
        "errorta_cli",
        "errorta_cli.serve",
        *_CLI_COMMAND_MODULES,
        # ---- Embedded sidecar (`errorta __serve__`) ---------------------- #
        # Everything below MUST mirror sidecar.spec: `__serve__` boots
        # errorta_app.server, so the frozen CLI carries the whole engine + AIAR.
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "errorta_app.server",
        # F065 mobile LAN TLS: cryptography is imported lazily inside
        # errorta_mobile.tls (function-body imports), so PyInstaller's static
        # scanner doesn't see it. Declare the package + the submodules the cert
        # generator uses, or the frozen binary can't make a TLS cert (the LAN
        # listener would fail closed when the operator enables it).
        "cryptography",
        "cryptography.x509",
        "cryptography.hazmat.backends.openssl",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.primitives.serialization",
        "cryptography.hazmat.primitives.hashes",
        # F-DIST-01 alpha licensing: Ed25519 token verify is lazy-imported inside
        # errorta_alpha.token, so declare it (and the package) or the frozen app
        # can't verify a license token when the alpha gate is on.
        "cryptography.hazmat.primitives.asymmetric.ed25519",
        "errorta_alpha",
        # Lazy-imported from the lifespan / routes when the gate is on, so the
        # static analyzer misses them: telemetry (slice 6), lifecycle (sync
        # loop), feedback (crash hook + bundle).
        "errorta_alpha.telemetry",
        "errorta_alpha.lifecycle",
        "errorta_alpha.feedback",
        "errorta_app.routes.alpha",
        "cffi",
        # F065 mobile connector packages (lazy-imported in routes/lifespan).
        "errorta_mobile",
        "errorta_app.mobile_server",
        "errorta_app.mobile_lifecycle",
        # F089 managed SSH tunnels — lazy-imported (function bodies in the
        # lifespan, settings route, and remote_config/remote_adapter), so the
        # static analyzer skips them; declare them or the frozen sidecar can't
        # bring up the watchdog tunnel.
        "errorta_tunnels",
        "errorta_tunnels.manager",
        # AIAR — every call site that touches it is lazy (inside function
        # bodies in errorta_app.health.aiar_pin, errorta_judge.aiar_adapter,
        # errorta_query.pipeline). PyInstaller's static analyzer skips
        # those by design, so the package and its used submodules must be
        # declared explicitly or the frozen sidecar reports
        # aiar_pin.source="absent" and the Council demo runs without
        # retrieval.
        "aiar",
        "aiar.harness",
        "aiar.harness.pipeline",
        "aiar.grounding",
        "aiar.grounding.store",
        # AIAR's optional-but-likely-needed hiddenimports. The aiar-rag[rag]==0.2.*
        # pin  pulls chromadb + sentence_transformers + fastapi as
        # runtime deps; PyInstaller's dynamic-import scanner misses them inside
        # AIAR's lazy-loaded paths, so they have to be declared explicitly.
        "chromadb",
        "sentence_transformers",
        "fastapi",
        # F147 CLI foreign-app detection (errorta_cli.sidecar._scan_errorta_processes)
        # imports psutil in a function body to spot a running Errorta.app /
        # errorta-sidecar before spawning a second sidecar. psutil is already
        # pulled in transitively by errorta_hwdetect, but declare it explicitly so
        # the sole-owner guard can never silently degrade in the frozen binary.
        "psutil",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Aggressively strip what we don't need from PyTorch's surface area.
        # (AIAR uses sentence-transformers which drags in torch — the GPU
        # variant of torch is enormous. These excludes are tuned over time.)
        "torch.utils.tensorboard",
        "torch.testing",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="errorta",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression breaks Apple notarization; leave off.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # A terminal CLI — must attach to the console for stdin/stdout.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("ERRORTA_CODESIGN_IDENTITY"),  # Developer ID Application string; unset -> unsigned.
    entitlements_file=os.environ.get("ERRORTA_ENTITLEMENTS_PLIST"),  # Path to macos/entitlements.plist; unset -> no entitlements.
)
