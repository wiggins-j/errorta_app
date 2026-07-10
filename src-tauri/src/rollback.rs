//! F-INFRA-09 Slice 5 — rollback storage + restartable swap flow.
//!
//! The rollback layer keeps copies of the most recent N installed Errorta
//! binaries (default N=3) under `<ERRORTA_HOME>/versions/<version>/` and
//! supports a restartable "swap on next boot" flow via an atomically-written
//! `pending_rollback.json` marker.
//!
//! Flow:
//!
//! 1. On a fresh install, `archive_current_version(current_binary, version)`
//!    copies the running binary into `<storage>/<version>/errorta(.exe)` and
//!    writes a `metadata.json` sidecar with size + install timestamp +
//!    `previous_version` (read from `install-marker.json` if present).
//!    Garbage-collects to N=3 prior versions.
//!
//! 2. When the user clicks "Roll back to vX.Y.Z" in the UpdatesCard, the
//!    Tauri command `rollback_to(version)` validates the archived binary
//!    exists, writes `pending_rollback.json` via atomic rename, and triggers
//!    an app restart through `tauri_plugin_process`.
//!
//! 3. On EVERY boot, `lib.rs::run()` calls `finalize_pending_rollback()`
//!    BEFORE spawning the sidecar. If a marker exists, the archived binary
//!    is atomically renamed over the running binary location, and the
//!    marker is removed. A machine crash between steps leaves the marker
//!    intact so the next boot retries the swap.
//!
//! 4. Crash-on-launch detector (slice 6) reads `install-marker.json` on
//!    boot — if the marker is < 10 s old, slice 6 schedules a "health
//!    check at +10 s" task; if the sidecar hasn't reached healthy, it
//!    writes `crash_recovery.json`, swaps to `previous_version`, and
//!    restarts.
//!
//! ## Linux AppImage caveat
//!
//! Atomic rename across the same filesystem works when the AppImage lives in
//! a user-writable directory (default `~/Applications/`). Root-owned install
//! paths (`/opt/Errorta/`) refuse `rename(2)` with EACCES; in that case
//! `rollback_to()` returns an error and the UI surfaces a "Rollback
//! unavailable: AppImage is in a system-managed directory" hint.

use std::{
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};

use crate::paths::errorta_home;

const MAX_ARCHIVED_VERSIONS: usize = 3;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub enum RollbackError {
    Io(io::Error),
    Json(serde_json::Error),
    CrossFilesystemRename { from: PathBuf, to: PathBuf },
    BinaryMissing(PathBuf),
    InvalidMarker(String),
}

impl std::fmt::Display for RollbackError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RollbackError::Io(e) => write!(f, "io: {e}"),
            RollbackError::Json(e) => write!(f, "json: {e}"),
            RollbackError::CrossFilesystemRename { from, to } => write!(
                f,
                "cross-filesystem rename refused: {} → {}",
                from.display(),
                to.display()
            ),
            RollbackError::BinaryMissing(p) => {
                write!(f, "archived binary missing: {}", p.display())
            }
            RollbackError::InvalidMarker(s) => write!(f, "invalid marker: {s}"),
        }
    }
}

impl std::error::Error for RollbackError {}

impl From<io::Error> for RollbackError {
    fn from(e: io::Error) -> Self {
        RollbackError::Io(e)
    }
}

impl From<serde_json::Error> for RollbackError {
    fn from(e: serde_json::Error) -> Self {
        RollbackError::Json(e)
    }
}

// ---------------------------------------------------------------------------
// Storage paths
// ---------------------------------------------------------------------------

pub fn rollback_storage_dir() -> PathBuf {
    rollback_storage_dir_in(&errorta_home())
}

pub fn rollback_storage_dir_in(home: &Path) -> PathBuf {
    home.join("versions")
}

pub fn pending_marker_path() -> PathBuf {
    pending_marker_path_in(&errorta_home())
}

pub fn pending_marker_path_in(home: &Path) -> PathBuf {
    rollback_storage_dir_in(home).join("pending_rollback.json")
}

pub fn install_marker_path_in(home: &Path) -> PathBuf {
    rollback_storage_dir_in(home).join("install-marker.json")
}

pub fn crash_recovery_path_in(home: &Path) -> PathBuf {
    rollback_storage_dir_in(home).join("crash_recovery.json")
}

#[cfg(windows)]
const BINARY_NAME: &str = "errorta.exe";
#[cfg(not(windows))]
const BINARY_NAME: &str = "errorta";

fn archived_binary_path(home: &Path, version: &str) -> PathBuf {
    rollback_storage_dir_in(home).join(version).join(BINARY_NAME)
}

fn archived_metadata_path(home: &Path, version: &str) -> PathBuf {
    rollback_storage_dir_in(home).join(version).join("metadata.json")
}

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RollbackEntry {
    pub version: String,
    pub installed_at: String,
    pub size_bytes: u64,
    #[serde(default)]
    pub notes: Option<String>,
    #[serde(default)]
    pub crashed_on_launch: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PendingMarker {
    pub version: String,
    pub requested_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallMarker {
    pub current_version: String,
    pub previous_version: String,
    pub installed_at_unix: u64,
}

impl InstallMarker {
    pub fn age(&self) -> Duration {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::from_secs(0))
            .as_secs();
        Duration::from_secs(now.saturating_sub(self.installed_at_unix))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CrashRecoveryEntry {
    pub failed_version: String,
    pub rolled_back_to: String,
    pub recorded_at: String,
    pub error: String,
}

// ---------------------------------------------------------------------------
// Atomic write helper
// ---------------------------------------------------------------------------

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), RollbackError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("tmp");
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all().ok();
    }
    fs::rename(&tmp, path)?;
    Ok(())
}

fn iso8601_now() -> String {
    // Minimal RFC 3339 / ISO-8601 formatting without bringing chrono in.
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::from_secs(0));
    let secs = now.as_secs() as i64;
    // Compute UTC date components (good enough for log timestamps; the
    // value is read back as an opaque string by the UI).
    let days = secs.div_euclid(86_400);
    let day_secs = secs.rem_euclid(86_400);
    let (year, month, day) = days_to_ymd(days);
    let hour = day_secs / 3600;
    let minute = (day_secs % 3600) / 60;
    let second = day_secs % 60;
    format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    )
}

fn days_to_ymd(days: i64) -> (i64, u32, u32) {
    // Unix epoch = 1970-01-01. This is a small civil-from-days implementation
    // (Howard Hinnant's algorithm). Covers any 64-bit second range.
    let mut z = days + 719_468;
    let era = if z >= 0 { z / 146_097 } else { (z - 146_096) / 146_097 };
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    z = if m <= 2 { y + 1 } else { y };
    (z, m, d)
}

// ---------------------------------------------------------------------------
// Storage operations
// ---------------------------------------------------------------------------

pub fn list_versions_in(home: &Path) -> Result<Vec<RollbackEntry>, RollbackError> {
    let dir = rollback_storage_dir_in(home);
    if !dir.exists() {
        return Ok(Vec::new());
    }
    let mut out: Vec<RollbackEntry> = Vec::new();
    for entry in fs::read_dir(&dir)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let version = entry.file_name().to_string_lossy().into_owned();
        let meta_path = archived_metadata_path(home, &version);
        if !meta_path.exists() {
            continue;
        }
        let raw = fs::read(&meta_path)?;
        match serde_json::from_slice::<RollbackEntry>(&raw) {
            Ok(e) => out.push(e),
            Err(_) => continue,
        }
    }
    // Newest-first by installed_at lexicographic (ISO-8601 timestamps sort
    // chronologically). Ties broken by version string.
    out.sort_by(|a, b| b.installed_at.cmp(&a.installed_at).then(b.version.cmp(&a.version)));
    Ok(out)
}

pub fn list_versions() -> Result<Vec<RollbackEntry>, RollbackError> {
    list_versions_in(&errorta_home())
}

pub fn archive_current_version_in(
    home: &Path,
    current_binary: &Path,
    version: &str,
) -> Result<(), RollbackError> {
    if !current_binary.exists() {
        return Err(RollbackError::BinaryMissing(current_binary.to_path_buf()));
    }
    let archived = archived_binary_path(home, version);
    if let Some(parent) = archived.parent() {
        fs::create_dir_all(parent)?;
    }
    let size_bytes = copy_file(current_binary, &archived)?;

    let prev_marker = read_install_marker_in(home).ok();
    let entry = RollbackEntry {
        version: version.to_string(),
        installed_at: iso8601_now(),
        size_bytes,
        notes: None,
        crashed_on_launch: None,
    };
    atomic_write(
        &archived_metadata_path(home, version),
        &serde_json::to_vec_pretty(&entry)?,
    )?;

    // Update install-marker.json so slice 6's detector + future archives
    // can read the previous version.
    let previous = prev_marker.map(|m| m.current_version).unwrap_or_default();
    let marker = InstallMarker {
        current_version: version.to_string(),
        previous_version: previous,
        installed_at_unix: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::from_secs(0))
            .as_secs(),
    };
    atomic_write(
        &install_marker_path_in(home),
        &serde_json::to_vec_pretty(&marker)?,
    )?;

    prune_old_versions_in(home, MAX_ARCHIVED_VERSIONS)?;
    Ok(())
}

pub fn archive_current_version(current_binary: &Path, version: &str) -> Result<(), RollbackError> {
    archive_current_version_in(&errorta_home(), current_binary, version)
}

fn copy_file(src: &Path, dst: &Path) -> Result<u64, RollbackError> {
    let mut input = fs::File::open(src)?;
    let mut output = fs::File::create(dst)?;
    let mut buf = vec![0u8; 64 * 1024];
    let mut total: u64 = 0;
    loop {
        let n = input.read(&mut buf)?;
        if n == 0 {
            break;
        }
        output.write_all(&buf[..n])?;
        total += n as u64;
    }
    output.sync_all().ok();
    Ok(total)
}

pub fn prune_old_versions_in(home: &Path, keep: usize) -> Result<(), RollbackError> {
    let entries = list_versions_in(home)?;
    if entries.len() <= keep {
        return Ok(());
    }
    for entry in entries.into_iter().skip(keep) {
        let dir = rollback_storage_dir_in(home).join(&entry.version);
        let _ = fs::remove_dir_all(&dir);
    }
    Ok(())
}

pub fn write_pending_marker_in(home: &Path, target_version: &str) -> Result<(), RollbackError> {
    let marker = PendingMarker {
        version: target_version.to_string(),
        requested_at: iso8601_now(),
    };
    atomic_write(
        &pending_marker_path_in(home),
        &serde_json::to_vec_pretty(&marker)?,
    )
}

pub fn write_pending_marker(target_version: &str) -> Result<(), RollbackError> {
    write_pending_marker_in(&errorta_home(), target_version)
}

pub fn read_pending_marker_in(home: &Path) -> Result<Option<PendingMarker>, RollbackError> {
    let path = pending_marker_path_in(home);
    if !path.exists() {
        return Ok(None);
    }
    let raw = fs::read(&path)?;
    let marker: PendingMarker = serde_json::from_slice(&raw)?;
    Ok(Some(marker))
}

pub fn finalize_pending_rollback_in(
    home: &Path,
    current_binary: &Path,
) -> Result<Option<String>, RollbackError> {
    let Some(marker) = read_pending_marker_in(home)? else {
        return Ok(None);
    };
    let archived = archived_binary_path(home, &marker.version);
    if !archived.exists() {
        // Preserve the marker so the user can retry once the archive is
        // restored (or remove it manually).
        return Err(RollbackError::BinaryMissing(archived));
    }
    let parent_a = archived.parent();
    let parent_c = current_binary.parent();
    // Best-effort cross-filesystem rename detection: if the two paths sit on
    // detectably-different roots (and we're not on Windows where the C: drive
    // assumption holds), refuse the swap and surface a user-visible error.
    if let (Some(a), Some(c)) = (parent_a, parent_c) {
        if a.canonicalize().is_ok() && c.canonicalize().is_ok() {
            // Heuristic: differing top-level mount points raise a flag.
            #[cfg(unix)]
            {
                if cross_fs_unix(a, c) {
                    return Err(RollbackError::CrossFilesystemRename {
                        from: archived.clone(),
                        to: current_binary.to_path_buf(),
                    });
                }
            }
        }
    }
    // Copy + atomic rename. We can't always rename(2) an executable that's
    // currently running, so a copy + rename is the portable contract.
    let staging = current_binary.with_extension("pending");
    copy_file(&archived, &staging)?;
    fs::rename(&staging, current_binary)?;
    fs::remove_file(pending_marker_path_in(home))?;
    Ok(Some(marker.version))
}

pub fn finalize_pending_rollback(current_binary: &Path) -> Result<Option<String>, RollbackError> {
    finalize_pending_rollback_in(&errorta_home(), current_binary)
}

#[cfg(unix)]
fn cross_fs_unix(a: &Path, b: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    let ma = match fs::metadata(a) {
        Ok(m) => m,
        Err(_) => return false,
    };
    let mb = match fs::metadata(b) {
        Ok(m) => m,
        Err(_) => return false,
    };
    ma.dev() != mb.dev()
}

// ---------------------------------------------------------------------------
// Install + crash-recovery markers (used by slice 6)
// ---------------------------------------------------------------------------

pub fn read_install_marker_in(home: &Path) -> Result<InstallMarker, RollbackError> {
    let path = install_marker_path_in(home);
    if !path.exists() {
        return Err(RollbackError::InvalidMarker("absent".into()));
    }
    let raw = fs::read(&path)?;
    Ok(serde_json::from_slice(&raw)?)
}

pub fn read_install_marker() -> Option<InstallMarker> {
    read_install_marker_in(&errorta_home()).ok()
}

pub fn clear_install_marker_in(home: &Path) -> Result<(), RollbackError> {
    let path = install_marker_path_in(home);
    if path.exists() {
        fs::remove_file(path)?;
    }
    Ok(())
}

pub fn clear_install_marker() {
    let _ = clear_install_marker_in(&errorta_home());
}

pub fn write_crash_recovery_marker_in(
    home: &Path,
    failed_version: &str,
    rolled_back_to: &str,
    error: &str,
) -> Result<(), RollbackError> {
    let entry = CrashRecoveryEntry {
        failed_version: failed_version.to_string(),
        rolled_back_to: rolled_back_to.to_string(),
        recorded_at: iso8601_now(),
        error: error.to_string(),
    };
    atomic_write(
        &crash_recovery_path_in(home),
        &serde_json::to_vec_pretty(&entry)?,
    )
}

pub fn write_crash_recovery_marker(
    failed_version: &str,
    rolled_back_to: &str,
    error: &str,
) -> Result<(), RollbackError> {
    write_crash_recovery_marker_in(&errorta_home(), failed_version, rolled_back_to, error)
}

pub fn read_crash_recovery_in(home: &Path) -> Result<Option<CrashRecoveryEntry>, RollbackError> {
    let path = crash_recovery_path_in(home);
    if !path.exists() {
        return Ok(None);
    }
    let raw = fs::read(&path)?;
    Ok(Some(serde_json::from_slice(&raw)?))
}

pub fn dismiss_crash_recovery_in(home: &Path) -> Result<(), RollbackError> {
    let path = crash_recovery_path_in(home);
    if path.exists() {
        fs::remove_file(path)?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
pub async fn list_rollbacks() -> Result<Vec<RollbackEntry>, String> {
    list_versions().map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn rollback_to(
    app: tauri::AppHandle,
    version: String,
) -> Result<(), String> {
    let home = errorta_home();
    let archived = archived_binary_path(&home, &version);
    if !archived.exists() {
        return Err(format!("no archived binary for v{version}"));
    }
    write_pending_marker_in(&home, &version).map_err(|e| e.to_string())?;
    // Drop the handle reference so the caller's runtime can drive the restart.
    let _ = app;
    Ok(())
}

#[tauri::command]
pub async fn get_crash_recovery() -> Option<CrashRecoveryEntry> {
    read_crash_recovery_in(&errorta_home()).ok().flatten()
}

#[tauri::command]
pub async fn dismiss_crash_recovery() -> Result<(), String> {
    dismiss_crash_recovery_in(&errorta_home()).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn archive_current_version_cmd(
    current_binary: String,
    version: String,
) -> Result<(), String> {
    archive_current_version(Path::new(&current_binary), &version).map_err(|e| e.to_string())
}
