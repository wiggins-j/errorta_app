# PyInstaller spec for the Errorta sidecar binary.
#
# Build with:
#   pyinstaller sidecar.spec
#
# Output: dist/errorta-sidecar (single executable)
#
# This is consumed by the Tauri build process: the sidecar binary is copied
# into src-tauri/binaries/ at build time and registered with
# tauri-plugin-shell as a child process.

# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# AIAR is installed editable (PEP 660 via setuptools) using a meta-path
# finder; PyInstaller's static Analyzer can't follow the custom finder so
# we resolve the AIAR source root from the installed finder file and add
# its parent to pathex so the `aiar` package gets included directly. The
# finder lives at .venv/lib/pythonX.Y/site-packages/__editable___aiar_*_finder.py
# and exposes a MAPPING dict pointing at the source dir.
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

# Build provenance: scripts/build-sidecar.sh writes errorta_app/_build_info.json
# from the git HEAD right before this spec runs, so the frozen sidecar reports
# its commit on /healthz (the stale-build check). Bundle it only if present —
# a plain `pyinstaller sidecar.spec` without the stamp still builds.
_build_info_datas = (
    [("errorta_app/_build_info.json", "errorta_app")]
    if os.path.isfile("errorta_app/_build_info.json") else []
)

a = Analysis(
    ["sidecar_main.py"],
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
        # generator uses, or the frozen .app can't make a TLS cert (the LAN
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
    name="errorta-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression breaks Apple notarization; leave off.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Sidecar logs to stdout; Tauri captures it.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("ERRORTA_CODESIGN_IDENTITY"),  # Developer ID Application string; unset -> unsigned.
    entitlements_file=os.environ.get("ERRORTA_ENTITLEMENTS_PLIST"),  # Path to macos/entitlements.plist; unset -> no entitlements.
)
