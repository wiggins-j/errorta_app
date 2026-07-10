// Auto-updater command surface.
//
// Two Tauri commands are exposed:
//
//   - `check_for_updates` — probe the configured manifest endpoint and
//     report `{status: "up_to_date" | "available" | "error" | "not_configured"}`.
//   - `install_update` — download + verify + install the available update;
//     report `{status: "installed" | "error" | "not_configured"}`.
//
// Both commands compile in TWO modes:
//
//   - Default (no `updater-enabled` feature) — return a stable
//     `{status: "not_configured", reason: ...}` payload. Frontend can call
//     these in dev without crashing and surfaces the v0.6-disabled hint.
//   - `--features updater-enabled` — delegate to `tauri-plugin-updater`,
//     which fetches the signed manifest, verifies the ed25519 signature,
//     and (on install_update) downloads and applies the new artifact.
//
// The feature-on path is INERT until v0.6 ships:
//   - A signing keypair is generated (see docs/AUTO_UPDATER.md).
//   - The public key is committed to tauri.conf.json → plugins.updater.pubkey.
//   - The errorta-downloads repo hosts a real manifest with .sig companions.
// Until those gates land, even a `--features updater-enabled` build cannot
// successfully verify a manifest. Activation is gated to Slice 7 of the
// F-INFRA-09 plan.

#[cfg(feature = "updater-enabled")]
use tauri_plugin_updater::UpdaterExt;

#[cfg(feature = "updater-enabled")]
#[tauri::command]
pub async fn check_for_updates(app: tauri::AppHandle) -> serde_json::Value {
    match app.updater() {
        Ok(updater) => match updater.check().await {
            Ok(Some(update)) => serde_json::json!({
                "status": "available",
                "version": update.version,
                "notes": update.body,
                "date": update.date.map(|d| d.to_string()),
            }),
            Ok(None) => serde_json::json!({
                "status": "up_to_date",
            }),
            Err(e) => serde_json::json!({
                "status": "error",
                "error": e.to_string(),
            }),
        },
        Err(e) => serde_json::json!({
            "status": "error",
            "error": format!("updater unavailable: {e}"),
        }),
    }
}

#[cfg(not(feature = "updater-enabled"))]
#[tauri::command]
pub fn check_for_updates() -> serde_json::Value {
    serde_json::json!({
        "status": "not_configured",
        "reason": "auto-update activates post-v0.6 signed release",
    })
}

#[cfg(feature = "updater-enabled")]
#[tauri::command]
pub async fn install_update(app: tauri::AppHandle) -> serde_json::Value {
    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => {
            return serde_json::json!({
                "status": "error",
                "error": format!("updater unavailable: {e}"),
            });
        }
    };
    let update_opt = match updater.check().await {
        Ok(opt) => opt,
        Err(e) => {
            return serde_json::json!({
                "status": "error",
                "error": e.to_string(),
            });
        }
    };
    let update = match update_opt {
        Some(u) => u,
        None => {
            return serde_json::json!({
                "status": "up_to_date",
            });
        }
    };
    // Progress events are dropped at the command boundary; UI subscribes to
    // the plugin's event stream directly in slice 4.
    match update
        .download_and_install(|_chunk, _total| {}, || {})
        .await
    {
        Ok(_) => serde_json::json!({
            "status": "installed",
            "version": update.version,
        }),
        Err(e) => serde_json::json!({
            "status": "error",
            "error": e.to_string(),
        }),
    }
}

#[cfg(not(feature = "updater-enabled"))]
#[tauri::command]
pub fn install_update() -> serde_json::Value {
    serde_json::json!({
        "status": "not_configured",
        "reason": "auto-update activates post-v0.6 signed release",
    })
}

/// Initialize the updater plugin on the given Tauri builder.
///
/// This is a no-op on default builds. With `--features updater-enabled`,
/// the real `tauri-plugin-updater` is wired in — but note the fallback
/// endpoint and inactive release feature flag mean even the
/// feature-enabled build won't fetch anything until the v0.6 gates pass.
#[cfg(feature = "updater-enabled")]
pub fn init_updater<R: tauri::Runtime>(
    builder: tauri::Builder<R>,
) -> tauri::Builder<R> {
    builder.plugin(tauri_plugin_updater::Builder::new().build())
}

#[cfg(not(feature = "updater-enabled"))]
pub fn init_updater<R: tauri::Runtime>(
    builder: tauri::Builder<R>,
) -> tauri::Builder<R> {
    builder
}
