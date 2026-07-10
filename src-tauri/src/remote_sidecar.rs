//! F-INFRA-12 Phase B Slice 7 — full SSH-remote tunnel lifecycle.
//!
//! Slice 5 introduced the `RemoteSidecarHandle` + `data_residency_mode` /
//! `set_data_residency` commands. Slice 6 added the read-only SSH probe
//! (`test_ssh_connection`) and a placeholder `install_remote_sidecar`. This
//! slice expands `set_data_residency(mode=ssh-remote)` into the full sequence:
//!
//!   1. Probe SSH (reuse the Slice 6 probe).
//!   2. If the sidecar is missing, `scp` the bundled binary to the remote and
//!      `chmod +x` it.
//!   3. Start the remote sidecar via `ssh <host> 'cd ~/.errorta && nohup env
//!      ERRORTA_SIDECAR_PORT=<remote_port> ./errorta-sidecar >sidecar.log 2>&1
//!      & echo $! > errorta-sidecar.pid; disown; sleep 1'` (the pidfile lets
//!      teardown kill exactly our process — F086 Slice F).
//!   4. Open an SSH local-forward tunnel `ssh -N -L
//!      127.0.0.1:<local_port>:127.0.0.1:<remote_port> <host>` (foreground -N
//!      so the child we own IS the real tunnel).
//!   5. Poll `http://127.0.0.1:<local_port>/healthz` until 200 or the 15 s
//!      budget runs out. On success, flip `tunnel_state` to `Up`.
//!   6. Spawn a background `watch_tunnel` task that re-probes `/healthz` every
//!      10 s. On failure it logs the transition, attempts ONE tunnel
//!      re-establish, and loops.
//!
//! Switching AWAY from `ssh-remote` (to `local` or `cloud`) cancels the
//! watcher, kills the owned local ssh-tunnel child, fires a best-effort remote
//! teardown that kills ONLY the recorded sidecar PID (from the pidfile), and
//! resets `tunnel_state` to `Down`.
//!
//! `RunEvent::Exit` in `lib.rs` invokes the same teardown path on app quit so
//! we don't leak the tunnel or the remote sidecar across restarts.

use std::path::PathBuf;
use std::process::{Child as StdChild, Command as StdCommand, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::Manager;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

use crate::paths::{
    self, ResidencyMode, ResidencyState, ResidencyStore, ResidencyWriteError,
};
use crate::sidecar::SidecarHandle;

/// Default port the remote sidecar binds on when no caller-supplied override
/// arrives via `ResidencyState.remote_sidecar_port`. Matches the dev-mode
/// default in `errorta_app/server.py::_resolve_port`.
const DEFAULT_REMOTE_SIDECAR_PORT: u16 = 8770;

/// How long Slice 7 will block waiting for the remote sidecar's `/healthz` to
/// reply through the tunnel before bailing out with `tunnel_state = Error`.
const HEALTHZ_BUDGET: Duration = Duration::from_secs(15);

/// Tunnel-watcher poll interval. Quoted in `watch_tunnel`'s docstring so the
/// frontend's `TunnelStatusBadge` "reconnecting" timeout matches.
const WATCHER_INTERVAL: Duration = Duration::from_secs(10);

/// Tunnel-state machine. Wire format is a tagged enum:
/// ```json
/// { "kind": "down" }
/// { "kind": "connecting" }
/// { "kind": "up" }
/// { "kind": "error", "detail": "ssh probe failed: ..." }
/// ```
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case", tag = "kind", content = "detail")]
pub enum TunnelState {
    #[default]
    Down,
    Connecting,
    Up,
    Error(String),
}

impl TunnelState {
    /// Short label for log lines. The transition log is part of the operator
    /// runbook; keep this stable.
    fn label(&self) -> String {
        match self {
            TunnelState::Down => "down".into(),
            TunnelState::Connecting => "connecting".into(),
            TunnelState::Up => "up".into(),
            TunnelState::Error(d) => format!("error({d})"),
        }
    }
}

/// In-process mirror of the active residency config plus the live tunnel +
/// remote-PID handles. The `tunnel_child` field owns the `std::process::Child`
/// returned by the foreground `ssh -N -L` spawn (F086 Slice F). Because we use
/// `-N` (NOT `-fN`), the captured `Child` IS the real ssh tunnel process — so a
/// teardown `.kill()` deterministically closes the tunnel, rather than killing
/// a self-forked wrapper that already exited and leaving the real tunnel to
/// leak.
pub struct RemoteSidecarHandle {
    pub mode: ResidencyMode,
    pub ssh_host: Option<String>,
    pub ssh_port: u16,
    pub ssh_key_path: Option<String>,
    pub ssh_username: Option<String>,
    pub remote_sidecar_port: Option<u16>,
    pub local_tunnel_port: Option<u16>,
    pub tunnel_state: TunnelState,
    /// Owned `ssh -N -L` child (the real tunnel process). None until
    /// open_tunnel populates it.
    pub tunnel_child: Option<StdChild>,
    /// Cancellation flag handed to the spawned watcher. Set to `true` to ask
    /// the watcher to exit on its next tick. Always replaced (not mutated)
    /// when a new watcher is spawned so stale references don't kill the new
    /// loop.
    pub watcher_stop: Option<Arc<AtomicBool>>,
    /// Mirror of the connection metadata the watcher needs to re-establish
    /// the tunnel on failure. Populated alongside `tunnel_child` in Slice 7.
    pub remote_child_pid: Option<u32>,
}

impl RemoteSidecarHandle {
    /// Construct from a freshly-loaded `ResidencyState`. `mode = local` pins
    /// `tunnel_state = Down` with no host/port mirrors set.
    pub fn new(state: &ResidencyState) -> Self {
        let mut handle = Self {
            mode: state.mode,
            ssh_host: None,
            ssh_port: 22,
            ssh_key_path: None,
            ssh_username: None,
            remote_sidecar_port: None,
            local_tunnel_port: None,
            tunnel_state: TunnelState::Down,
            tunnel_child: None,
            watcher_stop: None,
            remote_child_pid: None,
        };
        handle.apply_config_only(state);
        handle
    }

    /// Mirror the config-only fields from a fresh state. Does NOT touch the
    /// tunnel child, watcher flag, or `tunnel_state` — those are owned by the
    /// `set_data_residency` orchestration in Slice 7.
    fn apply_config_only(&mut self, state: &ResidencyState) {
        self.mode = state.mode;
        match state.mode {
            ResidencyMode::SshRemote => {
                self.ssh_host = state.ssh_host.clone();
                self.ssh_port = state.ssh_port;
                self.ssh_key_path = state.ssh_key_path.clone();
                self.ssh_username = state.ssh_username.clone();
                self.remote_sidecar_port = state.remote_sidecar_port;
            }
            ResidencyMode::Local | ResidencyMode::Cloud => {
                self.ssh_host = None;
                self.ssh_port = 22;
                self.ssh_key_path = None;
                self.ssh_username = None;
                self.remote_sidecar_port = None;
            }
        }
    }

    /// Cheap clone of the publicly-visible report shape.
    pub fn snapshot(&self) -> DataResidencyModeReport {
        DataResidencyModeReport {
            mode: self.mode,
            ssh_host: self.ssh_host.clone(),
            remote_sidecar_port: self.remote_sidecar_port,
            local_tunnel_port: self.local_tunnel_port,
            tunnel_state: self.tunnel_state.clone(),
        }
    }

    /// Transition log helper. Emits the operator-visible "tunnel state: X -> Y"
    /// line on every change and ONLY on actual changes — the watcher hits this
    /// every 10 s and we don't want spam.
    fn set_tunnel_state(&mut self, next: TunnelState) {
        if self.tunnel_state == next {
            return;
        }
        eprintln!(
            "[errorta] tunnel state: {} -> {}",
            self.tunnel_state.label(),
            next.label()
        );
        self.tunnel_state = next;
    }
}

/// Wire shape returned by `data_residency_mode` and `set_data_residency`.
#[derive(Debug, Clone, Serialize)]
pub struct DataResidencyModeReport {
    pub mode: ResidencyMode,
    pub ssh_host: Option<String>,
    pub remote_sidecar_port: Option<u16>,
    pub local_tunnel_port: Option<u16>,
    pub tunnel_state: TunnelState,
}

/// Tauri-managed wrapper around `RemoteSidecarHandle`. Held alongside
/// `ResidencyStore` in `app.manage(...)`.
pub struct RemoteSidecarStore {
    inner: Mutex<RemoteSidecarHandle>,
}

impl RemoteSidecarStore {
    pub fn new(state: &ResidencyState) -> Self {
        Self {
            inner: Mutex::new(RemoteSidecarHandle::new(state)),
        }
    }

    /// Take the lock or recover from poisoning. Mirrors the pattern in
    /// `sidecar.rs::SidecarHandle`. `pub(crate)` so the `RunEvent::Exit`
    /// handler in `lib.rs` can read the host/port snapshot before tearing
    /// down the in-memory handle.
    pub(crate) fn lock(&self) -> std::sync::MutexGuard<'_, RemoteSidecarHandle> {
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn snapshot(&self) -> DataResidencyModeReport {
        self.lock().snapshot()
    }

    /// Best-effort teardown. Called by `set_data_residency` when leaving
    /// ssh-remote mode and by `RunEvent::Exit` on app quit. Safe to call
    /// repeatedly.
    pub fn teardown(&self) {
        let (child, watcher) = {
            let mut guard = self.lock();
            guard.set_tunnel_state(TunnelState::Down);
            guard.local_tunnel_port = None;
            guard.remote_child_pid = None;
            (guard.tunnel_child.take(), guard.watcher_stop.take())
        };
        if let Some(flag) = watcher {
            flag.store(true, Ordering::SeqCst);
        }
        if let Some(mut c) = child {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

// ---------------------------------------------------------------------------
// Validation — mirrors `errorta_residency.config._validate` so the frontend
// has one error-mapping table. Strings are kept verbatim with the Python
// validator on purpose.
// ---------------------------------------------------------------------------

fn validate_residency_state(state: &ResidencyState) -> Result<(), String> {
    if !(1..=65535).contains(&state.ssh_port) {
        return Err(format!(
            "ssh_port must be an int in 1..65535, got {}",
            state.ssh_port
        ));
    }
    if matches!(state.remote_sidecar_port, Some(0)) {
        return Err(
            "remote_sidecar_port must be an int in 1..65535, got 0".to_string(),
        );
    }
    if matches!(state.local_tunnel_port, Some(0)) {
        return Err("local_tunnel_port must be an int in 1..65535, got 0".to_string());
    }
    match state.mode {
        ResidencyMode::SshRemote => {
            let ok = state
                .ssh_host
                .as_deref()
                .map(|s| !s.trim().is_empty())
                .unwrap_or(false);
            if !ok {
                return Err(
                    "ssh_host must be a non-empty string when mode='ssh-remote'".to_string(),
                );
            }
        }
        ResidencyMode::Cloud => {
            return Err(
                "Cloud data-residency mode is not enabled until token auth ships.".to_string(),
            );
        }
        ResidencyMode::Local => {}
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Tauri commands — `data_residency_mode` + `set_data_residency` (Slice 5/7).
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn data_residency_mode(
    handle: tauri::State<'_, RemoteSidecarStore>,
) -> DataResidencyModeReport {
    handle.snapshot()
}

/// Persist a new `ResidencyState` and refresh the in-memory handle.
///
/// Slice 7: when transitioning INTO `ssh-remote` mode, this command drives
/// the full probe → install → spawn → tunnel → healthz sequence and spawns
/// the watcher. On any sub-step failure, `tunnel_state` is set to `Error`
/// and the call returns `Err(detail)`.
///
/// When transitioning OUT of `ssh-remote`, the watcher is cancelled, the
/// tunnel child killed, and a best-effort remote pkill is fired.
#[tauri::command]
pub async fn set_data_residency<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    new_state: ResidencyState,
    store: tauri::State<'_, ResidencyStore>,
    remote: tauri::State<'_, RemoteSidecarStore>,
) -> Result<DataResidencyModeReport, String> {
    validate_residency_state(&new_state)?;

    // Always teardown an existing ssh-remote session before applying the new
    // state. Idempotent on local→local / cloud→cloud transitions.
    let was_ssh_remote = remote.lock().mode == ResidencyMode::SshRemote;
    if was_ssh_remote {
        remote.teardown();
        // Best-effort remote cleanup — fire-and-forget so a slow ssh round-trip
        // doesn't block mode-switching the UI.
        let (host, port, key, user) = {
            let g = remote.lock();
            (
                g.ssh_host.clone(),
                g.ssh_port,
                g.ssh_key_path.clone(),
                g.ssh_username.clone(),
            )
        };
        if let Some(h) = host {
            let app_for_pkill = app.clone();
            tauri::async_runtime::spawn(async move {
                let _ = run_remote_pkill(app_for_pkill, h, port, key, user).await;
            });
        }
    }

    // Pre-apply the config fields so the snapshot mirrors the new shape from
    // the moment Apply lands. The tunnel-state machine drives the rest.
    {
        let mut g = remote.lock();
        g.apply_config_only(&new_state);
        g.tunnel_state = TunnelState::Down;
        g.local_tunnel_port = None;
        g.tunnel_child = None;
        g.watcher_stop = None;
        g.remote_child_pid = None;
    }

    if new_state.mode == ResidencyMode::SshRemote {
        match bring_tunnel_up(app.clone(), &new_state, &remote).await {
            Ok(()) => {}
            Err(detail) => {
                remote.lock().set_tunnel_state(TunnelState::Error(detail.clone()));
                // Persist anyway so a future "Try again" can replay the same
                // config without losing the host the operator just typed.
                paths::write_residency(&new_state)
                    .map_err(|e: ResidencyWriteError| e.to_string())?;
                store.replace(new_state);
                return Err(detail);
            }
        }
    }

    let report = remote.lock().snapshot();
    let mut runtime_state = new_state;
    runtime_state.local_tunnel_port = report.local_tunnel_port;
    let mut persisted_state = runtime_state.clone();
    persisted_state.local_tunnel_port = None;
    paths::write_residency(&persisted_state).map_err(|e: ResidencyWriteError| e.to_string())?;
    store.replace(runtime_state);
    sync_python_residency(&app, &persisted_state, report.local_tunnel_port).await?;
    Ok(report)
}

/// Startup recovery: a persisted ssh-remote config intentionally has no
/// `local_tunnel_port` on disk. Recreate the live tunnel after the local sidecar
/// starts, then sync the fresh local port into Python's in-process residency
/// state via PUT /residency.
pub fn rehydrate_ssh_remote_on_startup<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    persisted_state: ResidencyState,
) {
    if persisted_state.mode != ResidencyMode::SshRemote {
        return;
    }

    tauri::async_runtime::spawn(async move {
        let Some(remote) = app.try_state::<RemoteSidecarStore>() else {
            eprintln!("[errorta] ssh-remote rehydrate skipped: remote store missing");
            return;
        };

        {
            let mut g = remote.lock();
            g.apply_config_only(&persisted_state);
            g.tunnel_state = TunnelState::Down;
            g.local_tunnel_port = None;
            g.tunnel_child = None;
            g.watcher_stop = None;
            g.remote_child_pid = None;
        }

        if let Err(e) = bring_tunnel_up(app.clone(), &persisted_state, &remote).await {
            remote.lock().set_tunnel_state(TunnelState::Error(e.clone()));
            eprintln!("[errorta] ssh-remote rehydrate failed: {e}");
            return;
        }

        let report = remote.lock().snapshot();
        let mut runtime_state = persisted_state.clone();
        runtime_state.local_tunnel_port = report.local_tunnel_port;
        if let Some(store) = app.try_state::<ResidencyStore>() {
            store.replace(runtime_state.clone());
        }
        if let Err(e) =
            sync_python_residency(&app, &persisted_state, report.local_tunnel_port).await
        {
            remote.lock().set_tunnel_state(TunnelState::Error(e.clone()));
            eprintln!("[errorta] ssh-remote residency sync failed: {e}");
        }
    });
}

async fn wait_for_local_sidecar_port<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    budget: Duration,
) -> Result<u16, String> {
    let started = Instant::now();
    loop {
        if let Some(sidecar) = app.try_state::<SidecarHandle>() {
            let port = sidecar.port();
            if port != 0 {
                return Ok(port);
            }
        }
        if started.elapsed() >= budget {
            return Err("local sidecar port was not available for residency sync".into());
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

async fn sync_python_residency<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    state: &ResidencyState,
    local_tunnel_port: Option<u16>,
) -> Result<(), String> {
    let sidecar_port = wait_for_local_sidecar_port(app, Duration::from_secs(15)).await?;
    let body = residency_sync_body(state, local_tunnel_port)?;
    post_json_to_local_sidecar(sidecar_port, "/residency", &body)
}

fn residency_sync_body(
    state: &ResidencyState,
    local_tunnel_port: Option<u16>,
) -> Result<String, String> {
    let payload = match state.mode {
        ResidencyMode::Local => serde_json::json!({ "mode": "local" }),
        ResidencyMode::SshRemote => serde_json::json!({
            "mode": "ssh-remote",
            "ssh_host": state.ssh_host.as_deref(),
            "ssh_port": state.ssh_port,
            "ssh_key_path": state.ssh_key_path.as_deref(),
            "ssh_username": state.ssh_username.as_deref(),
            "remote_sidecar_port": state.remote_sidecar_port,
            "local_tunnel_port": local_tunnel_port,
        }),
        ResidencyMode::Cloud => {
            return Err(
                "Cloud data-residency mode is not enabled until token auth ships.".into(),
            )
        }
    };
    serde_json::to_string(&payload).map_err(|e| format!("serialize residency sync body: {e}"))
}

fn post_json_to_local_sidecar(port: u16, path: &str, body: &str) -> Result<(), String> {
    let addr: std::net::SocketAddr = format!("127.0.0.1:{port}")
        .parse()
        .map_err(|e: std::net::AddrParseError| e.to_string())?;
    let mut stream = std::net::TcpStream::connect_timeout(&addr, Duration::from_secs(2))
        .map_err(|e| format!("connect local sidecar for residency sync: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    use std::io::{Read, Write};
    let req = format!(
        "PUT {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("write residency sync request: {e}"))?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|e| format!("read residency sync response: {e}"))?;
    if response.starts_with("HTTP/1.1 2") || response.starts_with("HTTP/1.0 2") {
        return Ok(());
    }
    let snippet: String = response.chars().take(300).collect();
    Err(format!("residency sync failed: {}", snippet.trim()))
}

/// Internal: drive the full ssh-remote bring-up sequence.
async fn bring_tunnel_up<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    new_state: &ResidencyState,
    remote: &tauri::State<'_, RemoteSidecarStore>,
) -> Result<(), String> {
    remote.lock().set_tunnel_state(TunnelState::Connecting);

    let host = new_state
        .ssh_host
        .clone()
        .ok_or_else(|| "ssh_host missing".to_string())?;
    let port = new_state.ssh_port;
    let key = new_state.ssh_key_path.clone();
    let user = new_state.ssh_username.clone();
    let remote_port = new_state
        .remote_sidecar_port
        .unwrap_or(DEFAULT_REMOTE_SIDECAR_PORT);

    // 1. Probe.
    let probe = run_ssh_probe(app.clone(), host.clone(), port, key.clone(), user.clone()).await?;

    // 2. Install if missing.
    if !probe.sidecar_present {
        install_remote_sidecar_impl(
            app.clone(),
            host.clone(),
            port,
            key.clone(),
            user.clone(),
        )
        .await?;
    }

    // 3. Spawn remote sidecar.
    spawn_remote_sidecar_impl(
        app.clone(),
        host.clone(),
        port,
        key.clone(),
        user.clone(),
        remote_port,
    )
    .await?;

    // 4. Open tunnel.
    let local_port = allocate_local_port()?;
    let child = open_tunnel(host.clone(), port, key.clone(), user.clone(), local_port, remote_port)
        .map_err(|e| format!("open_tunnel: {e}"))?;
    {
        let mut g = remote.lock();
        g.tunnel_child = Some(child);
        g.local_tunnel_port = Some(local_port);
    }

    // 5. Wait for /healthz through the tunnel.
    wait_for_remote_healthz(local_port, HEALTHZ_BUDGET)
        .map_err(|e| format!("healthz: {e}"))?;

    // 6. Mark Up + spawn watcher.
    remote.lock().set_tunnel_state(TunnelState::Up);
    let stop = Arc::new(AtomicBool::new(false));
    remote.lock().watcher_stop = Some(stop.clone());
    let conn = TunnelConn {
        host,
        port,
        key,
        user,
        local_port,
        remote_port,
    };
    spawn_watcher(app.clone(), conn, stop);
    Ok(())
}

// ---------------------------------------------------------------------------
// Slice 6 — `test_ssh_connection` + probe helpers. Carried forward verbatim.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SshProbeReport {
    pub uname: String,
    pub sidecar_present: bool,
    pub sidecar_version: Option<String>,
    pub raw_stdout: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InstallReport {
    pub already_installed: bool,
    pub version: Option<String>,
}

const PROBE_REMOTE_CMD: &str =
    "uname -sm && command -v errorta-sidecar >/dev/null 2>&1 && errorta-sidecar --version || echo NO_SIDECAR";

fn build_probe_args(
    host: &str,
    port: u16,
    key_path: Option<&str>,
    username: Option<&str>,
) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "-o".into(),
        "BatchMode=yes".into(),
        "-o".into(),
        "StrictHostKeyChecking=accept-new".into(),
        "-o".into(),
        "ConnectTimeout=5".into(),
    ];
    if port != 22 {
        args.push("-p".into());
        args.push(port.to_string());
    }
    if let Some(k) = key_path {
        if !k.is_empty() {
            args.push("-i".into());
            args.push(k.to_string());
        }
    }
    let target = match username {
        Some(u) if !u.is_empty() => format!("{u}@{host}"),
        _ => host.to_string(),
    };
    args.push(target);
    args.push(PROBE_REMOTE_CMD.to_string());
    args
}

fn parse_probe_stdout(raw_stdout: &str) -> SshProbeReport {
    let mut lines = raw_stdout
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty());
    let uname = lines.next().unwrap_or("").to_string();
    let second = lines.next().unwrap_or("");
    let (sidecar_present, sidecar_version) = if second == "NO_SIDECAR" || second.is_empty() {
        (false, None)
    } else {
        (true, Some(second.to_string()))
    };
    SshProbeReport {
        uname,
        sidecar_present,
        sidecar_version,
        raw_stdout: raw_stdout.to_string(),
    }
}

fn classify_ssh_stderr(stderr: &str) -> String {
    if stderr.contains("REMOTE HOST IDENTIFICATION HAS CHANGED") {
        return "host key changed".into();
    }
    if stderr.contains("Permission denied") {
        return "permission denied".into();
    }
    if stderr.contains("Connection refused") {
        return "connection refused".into();
    }
    if stderr.contains("Could not resolve") || stderr.contains("No route to host") {
        return "host unreachable".into();
    }
    let snippet: String = stderr.chars().take(200).collect();
    format!("unknown ssh error: {}", snippet.trim())
}

async fn run_ssh_probe<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
) -> Result<SshProbeReport, String> {
    let args = build_probe_args(&host, port, key_path.as_deref(), username.as_deref());
    let (mut rx, child) = match app.shell().command("ssh").args(args).spawn() {
        Ok(pair) => pair,
        Err(e) => {
            let msg = e.to_string();
            if msg.contains("not found") || msg.contains("No such file") {
                return Err("ssh binary not found".into());
            }
            return Err(format!("unknown ssh error: {msg}"));
        }
    };

    let child_slot: Arc<Mutex<Option<_>>> = Arc::new(Mutex::new(Some(child)));
    let timed_out = Arc::new(AtomicBool::new(false));
    let watchdog_slot = child_slot.clone();
    let watchdog_flag = timed_out.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(10));
        if let Some(c) = watchdog_slot.lock().unwrap_or_else(|p| p.into_inner()).take() {
            watchdog_flag.store(true, Ordering::SeqCst);
            let _ = c.kill();
        }
    });

    let mut stdout = Vec::<u8>::new();
    let mut stderr = Vec::<u8>::new();
    let mut exit_code: Option<i32> = None;
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(line) => {
                stdout.extend(line);
                stdout.push(b'\n');
            }
            CommandEvent::Stderr(line) => {
                stderr.extend(line);
                stderr.push(b'\n');
            }
            CommandEvent::Terminated(payload) => {
                exit_code = payload.code;
                break;
            }
            _ => {}
        }
    }
    let _ = child_slot.lock().unwrap_or_else(|p| p.into_inner()).take();

    if timed_out.load(Ordering::SeqCst) {
        return Err("ssh timed out".into());
    }

    let stdout_s = String::from_utf8_lossy(&stdout).to_string();
    let stderr_s = String::from_utf8_lossy(&stderr).to_string();

    match exit_code {
        Some(0) => Ok(parse_probe_stdout(&stdout_s)),
        Some(_) | None => Err(classify_ssh_stderr(&stderr_s)),
    }
}

#[tauri::command]
pub async fn test_ssh_connection<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
) -> Result<SshProbeReport, String> {
    run_ssh_probe(app, host, port, key_path, username).await
}

// ---------------------------------------------------------------------------
// Slice 7 — install_remote_sidecar real implementation.
// ---------------------------------------------------------------------------

/// Resolve the path to the bundled `errorta-sidecar-<triple>` binary on the
/// local laptop. Used as the source for scp.
///
/// In a packaged app the bundle layout puts the externalBin next to the main
/// app executable; in `tauri dev` it lives under `src-tauri/binaries/`. We try
/// the resource_dir() first (production) and fall back to the dev path. We do
/// NOT try to validate the binary's existence here — the scp invocation will
/// fail clearly if the path is wrong.
fn resolve_local_sidecar_path<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    let triple = target_triple();
    let bin_name = format!("errorta-sidecar-{triple}");

    // 1. Resource dir (production bundle).
    if let Ok(resource) = app.path().resource_dir() {
        let candidate = resource.join(&bin_name);
        if candidate.exists() {
            return Ok(candidate);
        }
    }

    // 2. Co-located with the main executable (some Tauri bundle layouts).
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let candidate = parent.join(&bin_name);
            if candidate.exists() {
                return Ok(candidate);
            }
        }
    }

    // 3. Dev path: src-tauri/binaries/ relative to the project root. Walk up
    //    from current_exe() until we find a `src-tauri/binaries` sibling.
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors() {
            let candidate = ancestor.join("src-tauri").join("binaries").join(&bin_name);
            if candidate.exists() {
                return Ok(candidate);
            }
        }
    }

    // 4. Last-ditch: well-known dev location.
    let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("binaries")
        .join(&bin_name);
    if dev.exists() {
        return Ok(dev);
    }

    Err(format!(
        "bundled sidecar binary `{bin_name}` not found; run scripts/build-sidecar.sh first"
    ))
}

/// Best-effort target-triple inference for the bundled sidecar name.
///
/// The bundled-binary suffix is the Rust target triple. `build.rs` plumbs
/// the build-time `TARGET` env var through as `TARGET_TRIPLE` so we can
/// read it back here.
///
/// Note: in cross-builds this is the BUILD HOST triple of the Tauri shell.
/// For v0.5 the host triple is the LOCAL machine (the laptop running the
/// GUI), which is generally NOT the same as the REMOTE machine's triple.
/// The scp install therefore needs the operator (or the build script) to
/// drop a `errorta-sidecar-<remote-triple>` binary into `src-tauri/binaries/`
/// before installing onto a Linux remote from a macOS laptop. This is
/// flagged in the operator runbook for slice 8.
fn target_triple() -> &'static str {
    env!("TARGET_TRIPLE")
}

/// scp argv. Order: [-P PORT] [-i KEY] <local_src> <remote_dest>. Mirrors the
/// `scp` scope entry in capabilities/default.json.
fn build_scp_args(
    local_src: &str,
    host: &str,
    remote_dest: &str,
    port: u16,
    key_path: Option<&str>,
    username: Option<&str>,
) -> Vec<String> {
    let mut args: Vec<String> = Vec::new();
    if port != 22 {
        args.push("-P".into());
        args.push(port.to_string());
    }
    if let Some(k) = key_path {
        if !k.is_empty() {
            args.push("-i".into());
            args.push(k.to_string());
        }
    }
    args.push(local_src.to_string());
    let target = match username {
        Some(u) if !u.is_empty() => format!("{u}@{host}:{remote_dest}"),
        _ => format!("{host}:{remote_dest}"),
    };
    args.push(target);
    args
}

/// Run a one-shot ssh remote command, return Ok(stdout) on exit 0.
async fn run_ssh_oneshot<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
    remote_cmd: String,
) -> Result<String, String> {
    let mut args: Vec<String> = vec![
        "-o".into(),
        "BatchMode=yes".into(),
        "-o".into(),
        "StrictHostKeyChecking=accept-new".into(),
        "-o".into(),
        "ConnectTimeout=5".into(),
    ];
    if port != 22 {
        args.push("-p".into());
        args.push(port.to_string());
    }
    if let Some(k) = key_path.as_deref() {
        if !k.is_empty() {
            args.push("-i".into());
            args.push(k.to_string());
        }
    }
    let target = match username.as_deref() {
        Some(u) if !u.is_empty() => format!("{u}@{host}"),
        _ => host.clone(),
    };
    args.push(target);
    args.push(remote_cmd);

    let (mut rx, _child) = app
        .shell()
        .command("ssh")
        .args(args)
        .spawn()
        .map_err(|e| format!("ssh spawn failed: {e}"))?;

    let mut stdout = Vec::<u8>::new();
    let mut stderr = Vec::<u8>::new();
    let mut exit_code: Option<i32> = None;
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(line) => {
                stdout.extend(line);
                stdout.push(b'\n');
            }
            CommandEvent::Stderr(line) => {
                stderr.extend(line);
                stderr.push(b'\n');
            }
            CommandEvent::Terminated(payload) => {
                exit_code = payload.code;
                break;
            }
            _ => {}
        }
    }
    let stdout_s = String::from_utf8_lossy(&stdout).to_string();
    let stderr_s = String::from_utf8_lossy(&stderr).to_string();
    match exit_code {
        Some(0) => Ok(stdout_s),
        Some(_) | None => Err(classify_ssh_stderr(&stderr_s)),
    }
}

/// Run a one-shot scp.
async fn run_scp<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    local_src: String,
    host: String,
    remote_dest: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
) -> Result<(), String> {
    let args = build_scp_args(
        &local_src,
        &host,
        &remote_dest,
        port,
        key_path.as_deref(),
        username.as_deref(),
    );
    let (mut rx, _child) = app
        .shell()
        .command("scp")
        .args(args)
        .spawn()
        .map_err(|e| format!("scp spawn failed: {e}"))?;

    let mut stderr = Vec::<u8>::new();
    let mut exit_code: Option<i32> = None;
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stderr(line) => {
                stderr.extend(line);
                stderr.push(b'\n');
            }
            CommandEvent::Terminated(payload) => {
                exit_code = payload.code;
                break;
            }
            _ => {}
        }
    }
    let stderr_s = String::from_utf8_lossy(&stderr).to_string();
    match exit_code {
        Some(0) => Ok(()),
        Some(c) => Err(format!("scp exit {c}: {}", classify_ssh_stderr(&stderr_s))),
        None => Err(format!("scp aborted: {}", classify_ssh_stderr(&stderr_s))),
    }
}

/// Real install path used by both `install_remote_sidecar` (Tauri command) and
/// the `bring_tunnel_up` orchestration in `set_data_residency`.
async fn install_remote_sidecar_impl<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
) -> Result<InstallReport, String> {
    let local_path = resolve_local_sidecar_path(&app)?;
    let local_path_s = local_path
        .to_str()
        .ok_or_else(|| "local sidecar path not utf-8".to_string())?
        .to_string();

    // 1. Ensure remote directory exists.
    run_ssh_oneshot(
        app.clone(),
        host.clone(),
        port,
        key_path.clone(),
        username.clone(),
        "mkdir -p ~/.errorta".into(),
    )
    .await
    .map_err(|e| format!("mkdir ~/.errorta failed: {e}"))?;

    // 2. scp the binary.
    run_scp(
        app.clone(),
        local_path_s,
        host.clone(),
        "~/.errorta/errorta-sidecar".into(),
        port,
        key_path.clone(),
        username.clone(),
    )
    .await
    .map_err(|e| format!("scp failed: {e}"))?;

    // 3. chmod + version-probe.
    let version_out = run_ssh_oneshot(
        app.clone(),
        host.clone(),
        port,
        key_path.clone(),
        username.clone(),
        "chmod +x ~/.errorta/errorta-sidecar && ~/.errorta/errorta-sidecar --version".into(),
    )
    .await
    .map_err(|e| format!("chmod/version probe failed: {e}"))?;

    let version = version_out
        .lines()
        .map(|l| l.trim())
        .find(|l| !l.is_empty())
        .map(|s| s.to_string());

    Ok(InstallReport {
        already_installed: false,
        version,
    })
}

/// Tauri command: install the Errorta sidecar on the remote host.
#[tauri::command]
pub async fn install_remote_sidecar<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
) -> Result<InstallReport, String> {
    // Probe first so an already-installed remote short-circuits.
    let probe = run_ssh_probe(
        app.clone(),
        host.clone(),
        port,
        key_path.clone(),
        username.clone(),
    )
    .await?;
    if probe.sidecar_present {
        return Ok(InstallReport {
            already_installed: true,
            version: probe.sidecar_version,
        });
    }
    install_remote_sidecar_impl(app, host, port, key_path, username).await
}

// ---------------------------------------------------------------------------
// Slice 7 — spawn_remote_sidecar.
// ---------------------------------------------------------------------------

/// Launch the python sidecar on the remote, backgrounded with nohup. Returns
/// Ok(()) once ssh exits — does NOT wait for /healthz here (the tunnel-up
/// step does that). The `sleep 1` gives the remote process time to bind
/// before the local-forward tunnel attempts a connect.
/// F086 Slice F: remote sidecar start command. Records the backgrounded PID to
/// `errorta-sidecar.pid` so teardown can kill exactly THIS process (and its
/// children) instead of a broad `pkill -f errorta-sidecar`.
fn remote_start_command(remote_sidecar_port: u16) -> String {
    format!(
        "cd ~/.errorta && nohup env ERRORTA_SIDECAR_PORT={remote_sidecar_port} \
         ./errorta-sidecar >sidecar.log 2>&1 & echo $! > errorta-sidecar.pid; \
         disown; sleep 1"
    )
}

/// F086 Slice F: targeted remote teardown — kills only the recorded sidecar PID
/// and its direct children (PyInstaller bootloader -> app fork). Never a broad
/// `pkill -f errorta-sidecar`.
fn remote_teardown_command() -> String {
    "PID=$(cat ~/.errorta/errorta-sidecar.pid 2>/dev/null); \
     if [ -n \"$PID\" ] && kill -0 \"$PID\" 2>/dev/null; then \
       pkill -P \"$PID\" 2>/dev/null || true; kill \"$PID\" 2>/dev/null || true; \
     fi; \
     rm -f ~/.errorta/errorta-sidecar.pid"
        .to_string()
}

async fn spawn_remote_sidecar_impl<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
    remote_sidecar_port: u16,
) -> Result<(), String> {
    let remote_cmd = remote_start_command(remote_sidecar_port);
    run_ssh_oneshot(app, host, port, key_path, username, remote_cmd)
        .await
        .map(|_| ())
        .map_err(|e| format!("spawn remote sidecar failed: {e}"))
}

/// Public Tauri command wrapper. The bring-up path calls `spawn_remote_sidecar_impl`
/// directly; this exists so a future "Restart remote sidecar" UI affordance can
/// re-trigger the spawn without flipping mode.
#[tauri::command]
pub async fn spawn_remote_sidecar<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
    remote_sidecar_port: Option<u16>,
) -> Result<(), String> {
    spawn_remote_sidecar_impl(
        app,
        host,
        port,
        key_path,
        username,
        remote_sidecar_port.unwrap_or(DEFAULT_REMOTE_SIDECAR_PORT),
    )
    .await
}

// ---------------------------------------------------------------------------
// Slice 7 — tunnel open / local port allocator / healthz wait / watcher.
// ---------------------------------------------------------------------------

/// Bind 127.0.0.1:0, read the assigned port, drop the listener. Race-condition
/// window between drop and ssh -L re-bind is acceptable for v0.5 (same trick
/// used by sidecar::allocate_free_port).
fn allocate_local_port() -> Result<u16, String> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")
        .map_err(|e| format!("could not bind ephemeral port: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("could not read ephemeral local addr: {e}"))?
        .port();
    drop(listener);
    Ok(port)
}

/// Spawn `ssh -N -L 127.0.0.1:<local_port>:127.0.0.1:<remote_port> <host>`.
///
/// F086 Slice F: foreground `-N` (NOT `-fN`). The spawned `Child` IS the real
/// ssh tunnel process — `spawn()` still returns immediately (it does not block
/// on `-f`), and we confirm the tunnel is up by polling a TCP connect to the
/// local forward port. Because we own the real process, a teardown `.kill()`
/// closes the tunnel deterministically instead of killing a self-forked wrapper
/// and leaking the actual tunnel.
///
/// Uses `std::process::Command` rather than the Tauri shell plugin because
/// the shell plugin's spawn machinery is geared toward live IO streaming and
/// the scope validators for `-fN -L ...` shapes are intentionally not exposed
/// to renderer code (the user can't invoke `open_tunnel` from JS — only the
/// `set_data_residency` orchestration does). This keeps the capability scope
/// surface area smaller.
fn open_tunnel(
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
    local_port: u16,
    remote_port: u16,
) -> Result<StdChild, String> {
    let mut cmd = StdCommand::new("ssh");
    cmd.arg("-o").arg("BatchMode=yes")
        .arg("-o").arg("StrictHostKeyChecking=accept-new")
        .arg("-o").arg("ConnectTimeout=5")
        .arg("-o").arg("ExitOnForwardFailure=yes")
        .arg("-o").arg("ServerAliveInterval=15")
        .arg("-o").arg("ServerAliveCountMax=3")
        // Foreground -N (not -fN): keep the real ssh as our owned child so a
        // teardown .kill() actually closes the tunnel (F086 Slice F).
        .arg("-N");
    if port != 22 {
        cmd.arg("-p").arg(port.to_string());
    }
    if let Some(k) = key_path.as_deref() {
        if !k.is_empty() {
            cmd.arg("-i").arg(k);
        }
    }
    cmd.arg("-L")
        .arg(format!("127.0.0.1:{local_port}:127.0.0.1:{remote_port}"));
    let target = match username.as_deref() {
        Some(u) if !u.is_empty() => format!("{u}@{host}"),
        _ => host.clone(),
    };
    cmd.arg(target);

    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("ssh spawn failed: {e}"))?;

    // Wait briefly for the local listener to come up. We poll a TCP connect
    // to 127.0.0.1:<local_port> with a 5 s budget.
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if std::net::TcpStream::connect_timeout(
            &format!("127.0.0.1:{local_port}")
                .parse()
                .map_err(|e: std::net::AddrParseError| e.to_string())?,
            Duration::from_millis(250),
        )
        .is_ok()
        {
            return Ok(child);
        }
        // If the parent has already died non-zero, surface stderr.
        if let Ok(Some(status)) = child.try_wait() {
            if !status.success() {
                let mut stderr_buf = String::new();
                if let Some(mut s) = child.stderr.take() {
                    use std::io::Read;
                    let _ = s.read_to_string(&mut stderr_buf);
                }
                return Err(format!(
                    "ssh -N -L exited with status {status:?}: {}",
                    classify_ssh_stderr(&stderr_buf)
                ));
            }
        }
        std::thread::sleep(Duration::from_millis(150));
    }

    let _ = child.kill();
    Err(format!(
        "tunnel did not come up on 127.0.0.1:{local_port} within 5s"
    ))
}

/// Block until `GET http://127.0.0.1:<local_port>/healthz` returns 2xx or the
/// budget elapses. Uses the same minimal HTTP/1.1 probe as `sidecar.rs` to
/// avoid pulling in reqwest as a direct dep.
fn wait_for_remote_healthz(local_port: u16, budget: Duration) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{local_port}/healthz");
    let start = Instant::now();
    loop {
        match probe_http(&url) {
            Ok(()) => return Ok(()),
            Err(e) => {
                if start.elapsed() >= budget {
                    return Err(format!(
                        "remote /healthz did not respond within {budget:?}: {e}"
                    ));
                }
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

/// Minimal blocking HTTP/1.1 GET to a known host:port/path. Returns Ok(()) if
/// the status line starts with `HTTP/1.{0,1} 2`. Mirrors `sidecar.rs::probe_http`.
fn probe_http(url: &str) -> Result<(), String> {
    let rest = url.strip_prefix("http://").ok_or("not http")?;
    let (host_port, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let addr: std::net::SocketAddr = host_port
        .parse()
        .map_err(|e: std::net::AddrParseError| e.to_string())?;
    let mut stream = std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(500))
        .map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_millis(1500)))
        .map_err(|e| e.to_string())?;
    use std::io::{Read, Write};
    let req = format!("GET {path} HTTP/1.1\r\nHost: {host_port}\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut buf = [0u8; 32];
    let n = stream.read(&mut buf).map_err(|e| e.to_string())?;
    let head = std::str::from_utf8(&buf[..n]).unwrap_or("");
    if head.starts_with("HTTP/1.1 2") || head.starts_with("HTTP/1.0 2") {
        Ok(())
    } else {
        Err(format!("non-2xx status line: {head:?}"))
    }
}

/// All the connection details the watcher needs to re-establish a dead tunnel.
#[derive(Clone)]
struct TunnelConn {
    host: String,
    port: u16,
    key: Option<String>,
    user: Option<String>,
    local_port: u16,
    remote_port: u16,
}

/// Spawn the periodic watcher task. The watcher polls /healthz every
/// `WATCHER_INTERVAL`, transitions the tunnel into `Error("healthz lost")` on
/// failure, attempts ONE re-establish, and stops when `stop` is tripped.
fn spawn_watcher<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    conn: TunnelConn,
    stop: Arc<AtomicBool>,
) {
    tauri::async_runtime::spawn(async move {
        loop {
            // Sleep first so an immediately-cancelled watcher (e.g. mode flip
            // milliseconds after Apply) exits without ever polling.
            tokio::time::sleep(WATCHER_INTERVAL).await;
            if stop.load(Ordering::SeqCst) {
                break;
            }

            let url = format!("http://127.0.0.1:{}/healthz", conn.local_port);
            let healthy = probe_http(&url).is_ok();

            // Pull a fresh handle to the managed state. If the store is gone
            // (mid app exit), bail.
            let Some(store) = app.try_state::<RemoteSidecarStore>() else {
                break;
            };

            if healthy {
                let mut g = store.lock();
                if g.tunnel_state != TunnelState::Up {
                    g.set_tunnel_state(TunnelState::Up);
                }
                continue;
            }

            // Unhealthy — mark Error and attempt ONE re-establish.
            {
                let mut g = store.lock();
                g.set_tunnel_state(TunnelState::Error("healthz lost".into()));
                if let Some(mut c) = g.tunnel_child.take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }

            if stop.load(Ordering::SeqCst) {
                break;
            }

            match open_tunnel(
                conn.host.clone(),
                conn.port,
                conn.key.clone(),
                conn.user.clone(),
                conn.local_port,
                conn.remote_port,
            ) {
                Ok(new_child) => {
                    // Give the new tunnel a moment, then re-probe.
                    tokio::time::sleep(Duration::from_millis(500)).await;
                    let recovered = probe_http(&url).is_ok();
                    let mut g = store.lock();
                    g.tunnel_child = Some(new_child);
                    if recovered {
                        g.set_tunnel_state(TunnelState::Up);
                    } else {
                        g.set_tunnel_state(TunnelState::Error(
                            "re-established tunnel but healthz still failing".into(),
                        ));
                    }
                }
                Err(e) => {
                    let mut g = store.lock();
                    g.set_tunnel_state(TunnelState::Error(format!(
                        "tunnel re-establish failed: {e}"
                    )));
                }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Remote cleanup — best-effort pkill on mode switch / app exit.
// ---------------------------------------------------------------------------

/// Public alias for the `RunEvent::Exit` handler in lib.rs. Same semantics
/// as `run_remote_pkill`.
pub async fn run_remote_pkill_public<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
) -> Result<(), String> {
    run_remote_pkill(app, host, port, key_path, username).await
}

/// Best-effort targeted teardown of the remote sidecar by its recorded PID.
///
/// F086 Slice F: kills ONLY the PID in `~/.errorta/errorta-sidecar.pid` (and its
/// direct children, for the PyInstaller bootloader -> app fork) — never a broad
/// `pkill -f errorta-sidecar`, which could reap an unrelated, same-named process
/// on a shared host. Failure is logged and swallowed.
async fn run_remote_pkill<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    host: String,
    port: u16,
    key_path: Option<String>,
    username: Option<String>,
) -> Result<(), String> {
    match run_ssh_oneshot(app, host, port, key_path, username, remote_teardown_command()).await {
        Ok(_) => Ok(()),
        Err(e) => {
            eprintln!("[errorta] remote pkill best-effort failed: {e}");
            Err(e)
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_present_sidecar() {
        let report = parse_probe_stdout("Linux x86_64\n0.5.0\n");
        assert_eq!(report.uname, "Linux x86_64");
        assert!(report.sidecar_present);
        assert_eq!(report.sidecar_version.as_deref(), Some("0.5.0"));
    }

    #[test]
    fn parses_missing_sidecar() {
        let report = parse_probe_stdout("Linux x86_64\nNO_SIDECAR\n");
        assert_eq!(report.uname, "Linux x86_64");
        assert!(!report.sidecar_present);
        assert!(report.sidecar_version.is_none());
    }

    #[test]
    fn parses_darwin_uname_with_version() {
        let report = parse_probe_stdout("Darwin arm64\n1.2.3\n");
        assert_eq!(report.uname, "Darwin arm64");
        assert!(report.sidecar_present);
        assert_eq!(report.sidecar_version.as_deref(), Some("1.2.3"));
    }

    // F086 Slice F — owned tunnel + targeted teardown.

    #[test]
    fn remote_start_records_pidfile() {
        let cmd = remote_start_command(8799);
        assert!(cmd.contains("ERRORTA_SIDECAR_PORT=8799"));
        // PID is recorded so teardown can target exactly our process.
        assert!(cmd.contains("echo $! > errorta-sidecar.pid"));
    }

    #[test]
    fn remote_teardown_targets_pid_not_broad_pkill() {
        let cmd = remote_teardown_command();
        // Kills only the recorded PID (+ its direct children), and cleans the file.
        assert!(cmd.contains("errorta-sidecar.pid"));
        assert!(cmd.contains("kill \"$PID\""));
        assert!(cmd.contains("pkill -P \"$PID\""));
        assert!(cmd.contains("rm -f ~/.errorta/errorta-sidecar.pid"));
        // The dangerous broad name-match must NOT reappear (would reap an
        // unrelated same-named process on a shared host).
        assert!(!cmd.contains("pkill -f errorta-sidecar"));
    }

    #[test]
    fn empty_stdout_yields_empty_uname_and_absent_sidecar() {
        let report = parse_probe_stdout("");
        assert_eq!(report.uname, "");
        assert!(!report.sidecar_present);
        assert!(report.sidecar_version.is_none());
        assert_eq!(report.raw_stdout, "");
    }

    #[test]
    fn raw_stdout_round_trips_verbatim() {
        let raw = "Linux x86_64\nNO_SIDECAR\n";
        let report = parse_probe_stdout(raw);
        assert_eq!(report.raw_stdout, raw);
    }

    #[test]
    fn build_args_no_port_no_key() {
        let args = build_probe_args("example-host", 22, None, None);
        assert_eq!(
            args,
            vec![
                "-o".to_string(),
                "BatchMode=yes".into(),
                "-o".into(),
                "StrictHostKeyChecking=accept-new".into(),
                "-o".into(),
                "ConnectTimeout=5".into(),
                "example-host".into(),
                PROBE_REMOTE_CMD.into(),
            ]
        );
    }

    #[test]
    fn build_args_with_port() {
        let args = build_probe_args("example-host", 2222, None, None);
        assert_eq!(args[6], "-p");
        assert_eq!(args[7], "2222");
        assert_eq!(args[8], "example-host");
        assert_eq!(args[9], PROBE_REMOTE_CMD);
    }

    #[test]
    fn build_args_with_key() {
        let args = build_probe_args("example-host", 22, Some("/Users/example/.ssh/id_ed25519"), None);
        assert_eq!(args[6], "-i");
        assert_eq!(args[7], "/Users/example/.ssh/id_ed25519");
        assert_eq!(args[8], "example-host");
    }

    #[test]
    fn build_args_with_username() {
        let args = build_probe_args("example-host", 22, None, Some("user"));
        assert_eq!(args[6], "user@example-host");
    }

    #[test]
    fn build_args_full_combo() {
        let args = build_probe_args(
            "example-host",
            2222,
            Some("/Users/example/.ssh/id_ed25519"),
            Some("user"),
        );
        assert_eq!(args.len(), 12);
        assert_eq!(args[6], "-p");
        assert_eq!(args[7], "2222");
        assert_eq!(args[8], "-i");
        assert_eq!(args[9], "/Users/example/.ssh/id_ed25519");
        assert_eq!(args[10], "user@example-host");
        assert_eq!(args[11], PROBE_REMOTE_CMD);
    }

    #[test]
    fn classify_permission_denied() {
        assert_eq!(
            classify_ssh_stderr("foo\nPermission denied (publickey).\nbar"),
            "permission denied"
        );
    }

    #[test]
    fn classify_connection_refused() {
        assert_eq!(
            classify_ssh_stderr("ssh: connect to host x port 22: Connection refused"),
            "connection refused"
        );
    }

    #[test]
    fn classify_host_unreachable() {
        assert_eq!(
            classify_ssh_stderr("ssh: Could not resolve hostname example-host: …"),
            "host unreachable"
        );
        assert_eq!(
            classify_ssh_stderr("ssh: connect: No route to host"),
            "host unreachable"
        );
    }

    #[test]
    fn classify_host_key_changed() {
        assert_eq!(
            classify_ssh_stderr(
                "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n\
                 @    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
            ),
            "host key changed"
        );
    }

    #[test]
    fn classify_unknown_truncates_stderr() {
        let stderr = "x".repeat(500);
        let msg = classify_ssh_stderr(&stderr);
        assert!(msg.starts_with("unknown ssh error: "));
        assert!(msg.len() <= "unknown ssh error: ".len() + 200);
    }

    // ----- Slice 7 additions -----

    #[test]
    fn allocate_local_port_returns_nonzero() {
        let p = allocate_local_port().expect("should allocate");
        assert!(p > 0);
        // Allocating again should also succeed and (almost certainly) return
        // a different port — the kernel cycles ephemerals.
        let p2 = allocate_local_port().expect("should allocate");
        assert!(p2 > 0);
    }

    #[test]
    fn scp_args_no_port_no_key() {
        let args = build_scp_args(
            "/local/sidecar",
            "example-host",
            "~/.errorta/errorta-sidecar",
            22,
            None,
            None,
        );
        assert_eq!(args.len(), 2);
        assert_eq!(args[0], "/local/sidecar");
        assert_eq!(args[1], "example-host:~/.errorta/errorta-sidecar");
    }

    #[test]
    fn scp_args_with_port_and_key() {
        let args = build_scp_args(
            "/local/sidecar",
            "example-host",
            "~/.errorta/errorta-sidecar",
            2222,
            Some("/Users/example/.ssh/id_ed25519"),
            Some("user"),
        );
        // -P 2222 -i KEY <src> <user@host:dst> -> 6 elements
        assert_eq!(args.len(), 6);
        assert_eq!(args[0], "-P");
        assert_eq!(args[1], "2222");
        assert_eq!(args[2], "-i");
        assert_eq!(args[3], "/Users/example/.ssh/id_ed25519");
        assert_eq!(args[4], "/local/sidecar");
        assert_eq!(args[5], "user@example-host:~/.errorta/errorta-sidecar");
    }

    #[test]
    fn tunnel_state_default_is_down() {
        assert_eq!(TunnelState::default(), TunnelState::Down);
    }

    #[test]
    fn tunnel_state_label_round_trips() {
        assert_eq!(TunnelState::Down.label(), "down");
        assert_eq!(TunnelState::Connecting.label(), "connecting");
        assert_eq!(TunnelState::Up.label(), "up");
        assert_eq!(
            TunnelState::Error("healthz lost".into()).label(),
            "error(healthz lost)"
        );
    }

    #[test]
    fn handle_apply_config_only_clears_on_local() {
        let mut state = ResidencyState::default();
        state.mode = ResidencyMode::SshRemote;
        state.ssh_host = Some("example-host".into());
        state.ssh_port = 2222;
        state.remote_sidecar_port = Some(8770);

        let mut h = RemoteSidecarHandle::new(&state);
        assert_eq!(h.ssh_host.as_deref(), Some("example-host"));
        assert_eq!(h.ssh_port, 2222);
        assert_eq!(h.remote_sidecar_port, Some(8770));

        let mut local = ResidencyState::default();
        local.mode = ResidencyMode::Local;
        h.apply_config_only(&local);
        assert!(h.ssh_host.is_none());
        assert_eq!(h.ssh_port, 22);
        assert!(h.remote_sidecar_port.is_none());
    }

    #[test]
    fn handle_set_tunnel_state_is_idempotent() {
        let h_state = ResidencyState::default();
        let mut h = RemoteSidecarHandle::new(&h_state);
        assert_eq!(h.tunnel_state, TunnelState::Down);
        // Idempotent — no spurious log on no-change.
        h.set_tunnel_state(TunnelState::Down);
        assert_eq!(h.tunnel_state, TunnelState::Down);
        h.set_tunnel_state(TunnelState::Connecting);
        assert_eq!(h.tunnel_state, TunnelState::Connecting);
        h.set_tunnel_state(TunnelState::Up);
        assert_eq!(h.tunnel_state, TunnelState::Up);
    }

    #[test]
    fn validate_residency_state_ssh_requires_host() {
        let mut s = ResidencyState::default();
        s.mode = ResidencyMode::SshRemote;
        s.ssh_host = None;
        let err = validate_residency_state(&s).unwrap_err();
        assert!(err.contains("ssh_host"));
    }

    #[test]
    fn validate_residency_state_cloud_is_disabled_until_auth_ships() {
        let mut s = ResidencyState::default();
        s.mode = ResidencyMode::Cloud;
        s.cloud_url = Some("http://insecure".into());
        let err = validate_residency_state(&s).unwrap_err();
        assert!(err.contains("not enabled"));
    }

    #[test]
    fn validate_residency_state_rejects_bad_port() {
        let mut s = ResidencyState::default();
        s.ssh_port = 0;
        let err = validate_residency_state(&s).unwrap_err();
        assert!(err.contains("ssh_port"));
    }

    #[test]
    fn residency_sync_body_local_clears_remote_fields() {
        let body = residency_sync_body(&ResidencyState::default(), None).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(parsed, serde_json::json!({ "mode": "local" }));
    }

    #[test]
    fn residency_sync_body_ssh_includes_live_tunnel_port() {
        let mut s = ResidencyState::default();
        s.mode = ResidencyMode::SshRemote;
        s.ssh_host = Some("example-host".into());
        s.ssh_port = 2222;
        s.ssh_username = Some("ops".into());
        s.remote_sidecar_port = Some(8770);
        s.local_tunnel_port = Some(12345);

        let body = residency_sync_body(&s, Some(18770)).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(parsed["mode"], "ssh-remote");
        assert_eq!(parsed["ssh_host"], "example-host");
        assert_eq!(parsed["ssh_port"], 2222);
        assert_eq!(parsed["ssh_username"], "ops");
        assert_eq!(parsed["remote_sidecar_port"], 8770);
        assert_eq!(parsed["local_tunnel_port"], 18770);
    }

    #[test]
    fn residency_state_ignores_stale_local_tunnel_port_on_deserialize() {
        let state: ResidencyState = serde_json::from_str(
            r#"{
                "mode": "ssh-remote",
                "ssh_host": "example-host",
                "ssh_port": 22,
                "remote_sidecar_port": 8770,
                "local_tunnel_port": 54321
            }"#,
        )
        .unwrap();
        assert_eq!(state.mode, ResidencyMode::SshRemote);
        assert_eq!(state.ssh_host.as_deref(), Some("example-host"));
        assert_eq!(state.local_tunnel_port, None);
    }
}
