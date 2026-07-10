// System tray icon + menu for Errorta.
//
// Closing the main window hides it instead of quitting (see
// `WindowEvent::CloseRequested` in lib.rs). The tray icon is the only way to
// bring the window back without re-launching the app, and the only way to
// fully quit (which terminates the Python sidecar via `RunEvent::Exit`).
//
// Menu items:
//   - "Show Errorta"                     → restore + focus the main window
//   - "Check for updates (coming soon)"  → DISABLED placeholder (F-INFRA-09 / v0.6)
//   - "Quit Errorta"                     → `app.exit(0)` → triggers RunEvent::Exit
//
// Left-clicking the tray icon itself also restores the window (parity with
// most native tray apps on macOS / Windows / Linux).

use tauri::{
    menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};

/// Build the tray icon and attach it to the running app.
///
/// Errors bubble up so the caller (lib.rs setup closure) can log them to
/// stderr without panicking — a missing tray is degraded UX, not a fatal
/// failure (the app remains usable via the main window).
pub fn build_tray(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let show_item = MenuItemBuilder::with_id("show", "Show Errorta").build(app)?;
    let check_updates_item = MenuItemBuilder::with_id("check_updates", "Check for updates (coming soon)")
        .enabled(false)
        .build(app)?;
    let quit_item = MenuItemBuilder::with_id("quit", "Quit Errorta").build(app)?;

    let menu = MenuBuilder::new(app)
        .item(&show_item)
        .item(&PredefinedMenuItem::separator(app)?)
        .item(&check_updates_item)
        .item(&PredefinedMenuItem::separator(app)?)
        .item(&quit_item)
        .build()?;

    let mut builder = TrayIconBuilder::with_id("errorta-tray")
        .tooltip("Errorta")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.unminimize();
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            "quit" => {
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.unminimize();
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
        });

    // macOS menu-bar icons are TEMPLATE images: macOS uses only the alpha
    // channel and tints the shape (black in light bars, white in dark). The
    // full-color window icon is fully opaque, so as a template it renders as a
    // solid white SQUARE. So on macOS we load a dedicated monochrome cloud+E
    // template (black-on-transparent). Other platforms keep the colored window
    // icon, which their trays render in full color.
    #[cfg(target_os = "macos")]
    {
        match tauri::image::Image::from_bytes(include_bytes!("../icons/tray-template@2x.png")) {
            Ok(icon) => {
                builder = builder.icon(icon).icon_as_template(true);
            }
            Err(err) => {
                eprintln!("[errorta] tray: template icon load failed ({err}); falling back");
                if let Some(icon) = app.default_window_icon().cloned() {
                    builder = builder.icon(icon).icon_as_template(true);
                }
            }
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        // Prefer the bundled window icon (wired through tauri.conf.json's
        // bundle.icon array). A missing icon indicates a packaging regression —
        // surface it loudly but continue iconless rather than panic.
        if let Some(icon) = app.default_window_icon().cloned() {
            builder = builder.icon(icon);
        } else {
            eprintln!("[errorta] tray: no default window icon available; tray will render iconless");
        }
    }

    builder.build(app)?;
    Ok(())
}
