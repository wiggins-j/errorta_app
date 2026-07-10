// Errorta Tauri shell.
//
// Hosts the React/Vite frontend in a system webview and manages the Python
// sidecar lifecycle. On app startup we allocate a free port, spawn the
// PyInstaller-built `errorta-sidecar` binary with that port in its env, and
// poll `/healthz` until it responds. The frontend reads the resolved port via
// the `sidecar_port` Tauri command.

mod paths;
mod remote_sidecar;
pub mod rollback;
mod shell_cmds;
mod sidecar;
mod tray;
mod updater;

use tauri::{AppHandle, Manager, RunEvent, WindowEvent};

use paths::{ResidencyState, ResidencyStore};
use remote_sidecar::RemoteSidecarStore;
use sidecar::SidecarHandle;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_process::init());
    // Wire the auto-updater scaffold. No-op on default builds; behind the
    // `updater-enabled` feature flag this attaches `tauri-plugin-updater`.
    // INERT until v0.6 — see docs/AUTO_UPDATER.md.
    let builder = updater::init_updater(builder);
    let app = builder
        .manage(SidecarHandle::new())
        .setup(|app| {
            // F-INFRA-09 Slice 5 — finalize any pending rollback BEFORE any
            // sidecar work. Restartable: a crash between marker-write and
            // finalize leaves the next boot in a valid finalize-able state.
            if let Ok(current_exe) = std::env::current_exe() {
                match rollback::finalize_pending_rollback(&current_exe) {
                    Ok(Some(v)) => eprintln!("[errorta] rollback finalized to v{v}"),
                    Ok(None) => {}
                    Err(e) => {
                        eprintln!("[errorta] pending-rollback finalize failed: {e}");
                    }
                }
            }
            let handle = app.handle().clone();
            // Spawn on a background thread so a slow PyInstaller cold-start
            // doesn't block the splash. Errors are logged but non-fatal in
            // dev — the user can still run the sidecar manually on 8770.
            std::thread::spawn(move || {
                let spawn_result = sidecar::spawn_sidecar(&handle);
                match &spawn_result {
                    Ok(port) => eprintln!("[errorta] sidecar healthy on 127.0.0.1:{port}"),
                    Err(e) => eprintln!("[errorta] sidecar spawn failed: {e}"),
                }
                // F-INFRA-09 Slice 6 — crash-on-launch detector. If a recent
                // install marker is present (<10s old), schedule a
                // post-window watchdog: if the sidecar is healthy 10s later,
                // clear the marker; otherwise, write crash_recovery.json,
                // queue a rollback to the previous version, and restart.
                if let Some(marker) = rollback::read_install_marker() {
                    if marker.age() < std::time::Duration::from_secs(10)
                        && !marker.previous_version.is_empty()
                    {
                        let healthy_now = spawn_result.is_ok();
                        let prev = marker.previous_version.clone();
                        let failed = marker.current_version.clone();
                        std::thread::spawn(move || {
                            std::thread::sleep(std::time::Duration::from_secs(10));
                            if healthy_now {
                                rollback::clear_install_marker();
                            } else {
                                let _ = rollback::write_crash_recovery_marker(
                                    &failed,
                                    &prev,
                                    "sidecar failed to reach healthy within 10s",
                                );
                                let _ = rollback::write_pending_marker(&prev);
                                eprintln!(
                                    "[errorta] crash-on-launch detected for v{failed}; queued rollback to v{prev}"
                                );
                            }
                        });
                    }
                }
            });
            // Resolve the persisted data-residency state. Missing file is
            // expected on a fresh install and falls back to the default
            // (mode: local). A malformed or otherwise-unreadable file logs
            // and falls back rather than blocking app boot — Slice 5's
            // Settings UI is the canonical recovery path.
            let residency = match paths::read_residency() {
                Ok(state) => state,
                Err(paths::ResidencyReadError::Malformed(e)) => {
                    eprintln!(
                        "[errorta] data-residency.json malformed, using default: {e}"
                    );
                    ResidencyState::default()
                }
                Err(paths::ResidencyReadError::Io(e)) => {
                    eprintln!(
                        "[errorta] data-residency.json read failed, using default: {e}"
                    );
                    ResidencyState::default()
                }
            };
            eprintln!(
                "[errorta] data residency mode: {}",
                residency.mode.as_kebab()
            );
            let rehydrate_residency = residency.clone();
            app.manage(RemoteSidecarStore::new(&residency));
            app.manage(ResidencyStore::new(residency));
            remote_sidecar::rehydrate_ssh_remote_on_startup(
                app.handle().clone(),
                rehydrate_residency,
            );
            // Wire the system tray. A failure here is non-fatal — the user
            // still has the main window. See docs/SYSTEM_TRAY.md.
            if let Err(e) = tray::build_tray(app.handle()) {
                eprintln!("[errorta] tray init failed: {e}");
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            about,
            quit_app,
            shell_cmds::shell_ping,
            shell_cmds::open_logs_folder,
            shell_cmds::open_path,
            shell_cmds::launch_cli_login,
            shell_cmds::cli_login_launch_available,
            sidecar::sidecar_port,
            sidecar::sidecar_startup_state,
            sidecar::restart_sidecar,
            sidecar::ensure_sidecar,
            sidecar::processes,
            remote_sidecar::data_residency_mode,
            remote_sidecar::set_data_residency,
            remote_sidecar::test_ssh_connection,
            remote_sidecar::install_remote_sidecar,
            remote_sidecar::spawn_remote_sidecar,
            updater::check_for_updates,
            updater::install_update,
            rollback::list_rollbacks,
            rollback::rollback_to,
            rollback::get_crash_recovery,
            rollback::dismiss_crash_recovery,
            rollback::archive_current_version_cmd,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Errorta");

    app.run(|app_handle, event| match event {
        // True app exit (Quit menu / Cmd+Q / app.exit()). Sidecar shutdown
        // lives here so a hide-on-close does NOT kill the Python process.
        RunEvent::Exit => {
            // Tear down the SSH-remote tunnel (Slice 7) — cancels the watcher,
            // kills the local ssh-tunnel child, and fires a best-effort remote
            // pkill so the remote python sidecar doesn't outlive us.
            if let Some(remote) = app_handle.try_state::<RemoteSidecarStore>() {
                let (host, port, key, user) = {
                    let g = remote.lock();
                    (
                        g.ssh_host.clone(),
                        g.ssh_port,
                        g.ssh_key_path.clone(),
                        g.ssh_username.clone(),
                    )
                };
                remote.teardown();
                if let Some(h) = host {
                    // Best-effort, fire-and-forget. Exit doesn't wait on it —
                    // the OS will reap on process death.
                    let app_for_pkill = app_handle.clone();
                    tauri::async_runtime::spawn(async move {
                        let _ = remote_sidecar::run_remote_pkill_public(
                            app_for_pkill,
                            h,
                            port,
                            key,
                            user,
                        )
                        .await;
                    });
                }
            }
            if let Some(handle) = app_handle.try_state::<SidecarHandle>() {
                handle.terminate();
            }
        }
        // Window close → hide to tray instead of quitting. The tray's
        // "Quit Errorta" menu item is the canonical way to fully exit.
        RunEvent::WindowEvent {
            label,
            event: WindowEvent::CloseRequested { api, .. },
            ..
        } if label == "main" => {
            if let Some(window) = app_handle.get_webview_window(&label) {
                api.prevent_close();
                let _ = window.hide();
            }
        }
        _ => {}
    });
}

#[tauri::command]
fn about() -> serde_json::Value {
    serde_json::json!({
        "name": env!("CARGO_PKG_NAME"),
        "version": env!("CARGO_PKG_VERSION"),
        "description": env!("CARGO_PKG_DESCRIPTION"),
    })
}

/// F103 — fully exit the app from the startup splash's failure state. Unlike
/// the window close button (which hides to tray), this triggers `RunEvent::Exit`
/// so the sidecar (and any SSH-remote tunnel) is torn down cleanly.
#[tauri::command]
fn quit_app(app: AppHandle) {
    app.exit(0);
}
