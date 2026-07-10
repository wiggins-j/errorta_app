# System tray — close/hide/quit semantics

> Track: **F006-TRAY** (part of the F006 Tauri shell). Landed in v0.1.

Errorta runs as a tray-resident desktop app. The tray icon is the
canonical surface for restoring the window and for fully quitting the app.

---

## Behaviors

| Action | Result |
|---|---|
| Click the window's close button (`X` on Win/Linux, red dot on macOS) | Main window **hides**. App and Python sidecar keep running. |
| Left-click the tray icon | Main window **shows + focuses**. |
| Tray menu → **Show Errorta** | Main window **shows + focuses**. |
| Tray menu → **Check for updates (coming soon)** | **Disabled** placeholder. See §Updater. |
| Tray menu → **Quit Errorta** | App calls `app.exit(0)` → triggers `RunEvent::Exit` → Python sidecar is terminated → process exits. |
| Cmd+Q (macOS) / Alt+F4 (Win/Linux) on the window | Currently routes through `WindowEvent::CloseRequested` and is treated as "hide". To fully quit, use the tray. *(This is intentional v0.1 behavior — Cmd+Q-to-quit may be wired in a later track.)* |

## How it's wired (Rust)

- **`src-tauri/src/tray.rs`** — `build_tray(&AppHandle)` constructs the tray icon
  and menu. Called from the existing `.setup()` closure in `lib.rs` after the
  sidecar spawn. Errors are logged to stderr — a missing tray degrades UX but
  does not crash the app.
- **`src-tauri/src/lib.rs`** — `RunEvent::WindowEvent { CloseRequested }` for the
  `main` window calls `api.prevent_close()` then `window.hide()`. Sidecar
  termination has been moved out of `CloseRequested` and lives **only** in
  `RunEvent::Exit`, so a hide-to-tray never kills the Python process.
- **`src-tauri/Cargo.toml`** — the `tauri` crate now enables the `tray-icon`
  and `image-png` features (required by Tauri 2 to compile `tauri::tray`).

## Updater placeholder

The **Check for updates (coming soon)** menu item is rendered but disabled.
Wiring it to a real check is deferred to **v0.6** along with the rest of the
auto-updater work (see `docs/AUTO_UPDATER.md` and roadmap track
`F-INFRA-09`). When that lands, this item will:

1. Become enabled.
2. Invoke `updater::check_for_updates` (the existing scaffold command).
3. Surface a native dialog with version/changelog if an update is available.

Until then, the disabled state is intentional — it signals to users that
updates are coming without misleading them about current functionality.

## Future work (not in v0.1)

- Dynamic tray tooltip showing sidecar health (green/red dot).
- "Restart sidecar" menu item.
- Notification badges (judge verdicts requiring attention).
- macOS Dock-icon hide on window-hide (currently the Dock icon stays visible).
