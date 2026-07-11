fn main() {
    // Forward the build-time TARGET triple (e.g. aarch64-apple-darwin,
    // x86_64-unknown-linux-gnu) into the compiled binary as TARGET_TRIPLE.
    // Used by `remote_sidecar::resolve_local_sidecar_path` to find the
    // bundled `errorta-sidecar-<triple>` binary on disk.
    let target = std::env::var("TARGET").unwrap_or_default();
    println!("cargo:rustc-env=TARGET_TRIPLE={target}");
    println!("cargo:rerun-if-env-changed=TARGET");

    // F147 S9b — stamp the build commit (git HEAD) so the running app can
    // POSITIVELY confirm an already-running sidecar advertised in
    // ${ERRORTA_HOME}/sidecar.json is the SAME build before ADOPTING it
    // (single-instance adoption). This matches the full HEAD that
    // scripts/build-sidecar.sh stamps into the sidecar's _build_info.json, so a
    // normal `rebuild-app.sh` (which builds both from one clean HEAD) makes the
    // two commits equal. `ERRORTA_BUILD_COMMIT` env wins if a build sets it.
    // Empty (a source tarball with no git) ⇒ the app can't confirm a match and
    // therefore never adopts — it always spawns its own sidecar (safe fallback).
    let commit = std::env::var("ERRORTA_BUILD_COMMIT")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            std::process::Command::new("git")
                .args(["rev-parse", "HEAD"])
                .output()
                .ok()
                .filter(|o| o.status.success())
                .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
                .filter(|s| !s.is_empty())
        })
        .unwrap_or_default();
    println!("cargo:rustc-env=ERRORTA_BUILD_COMMIT={commit}");
    println!("cargo:rerun-if-env-changed=ERRORTA_BUILD_COMMIT");
    // F147 S9 follow-up (review NIT-6): also re-run this script when git HEAD
    // moves, so the stamp refreshes across commits.
    emit_git_rerun_hints();

    tauri_build::build()
}

/// F147 S9 follow-up (review NIT-6) — refresh the `ERRORTA_BUILD_COMMIT` stamp
/// when git HEAD moves.
///
/// Without a `cargo:rerun-if-changed`, cargo caches `build.rs`'s output whenever
/// nothing under `src-tauri/` changes, so a plain `git commit` left the stamped
/// commit STALE — a dev build then never matched a freshly built sidecar's
/// `_build_info.json`, so single-instance ADOPTION never engaged. Declaring
/// `rerun-if-changed` for the files git rewrites on a commit/checkout makes cargo
/// re-run this script (and re-stamp `git rev-parse HEAD`) the moment HEAD moves.
///
/// Fully defensive: no `.git` metadata, a detached HEAD, or any unreadable file
/// simply means fewer/zero rerun hints — cargo keeps the cached stamp, which is
/// the safe, pre-existing fallback (a stale stamp only makes adoption safe-fall
/// back to "spawn our own", never corrupts anything). Never fails the build.
fn emit_git_rerun_hints() {
    let Some(git_dir) = find_git_dir() else {
        return;
    };
    let common_dir = find_common_git_dir(&git_dir);
    let mut emitted = std::collections::BTreeSet::new();
    // `.git/HEAD` is rewritten on every branch switch / detached checkout.
    let head = git_dir.join("HEAD");
    emit_rerun_hint(&head, &mut emitted);
    // `.git/packed-refs` — a fallback for a ref stored packed rather than loose
    // (e.g. right after a `git gc` or a fresh clone).
    emit_rerun_hint_if_file(git_dir.join("packed-refs"), &mut emitted);
    emit_rerun_hint_if_file(common_dir.join("packed-refs"), &mut emitted);
    // The current branch's loose ref file (e.g. `.git/refs/heads/<branch>`) is
    // what git rewrites when you commit on that branch. Resolve it from HEAD's
    // `ref: refs/...` target. A detached HEAD (raw sha, no `ref:` prefix) has no
    // branch ref — HEAD itself already changes on any move, so it's covered.
    if let Ok(contents) = std::fs::read_to_string(&head) {
        if let Some(target) = contents.strip_prefix("ref:").map(str::trim) {
            if !target.is_empty() {
                emit_rerun_hint_if_file(git_dir.join(target), &mut emitted);
                emit_rerun_hint_if_file(common_dir.join(target), &mut emitted);
            }
        }
    }
}

fn emit_rerun_hint(path: &std::path::Path, emitted: &mut std::collections::BTreeSet<String>) {
    let key = path.display().to_string();
    if emitted.insert(key.clone()) {
        println!("cargo:rerun-if-changed={key}");
    }
}

fn emit_rerun_hint_if_file(
    path: std::path::PathBuf,
    emitted: &mut std::collections::BTreeSet<String>,
) {
    if path.is_file() {
        emit_rerun_hint(&path, emitted);
    }
}

/// Walk up from `CARGO_MANIFEST_DIR` looking for git metadata. Supports both the
/// normal `.git` directory and the `.git` file used by linked worktrees
/// (`gitdir: ...`). Returns the worktree-specific git dir, or `None` if not
/// found/unreadable.
fn find_git_dir() -> Option<std::path::PathBuf> {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").ok()?;
    let mut dir = std::path::PathBuf::from(manifest);
    loop {
        let candidate = dir.join(".git");
        if candidate.is_dir() {
            return Some(candidate);
        }
        if candidate.is_file() {
            if let Some(path) = read_gitdir_file(&candidate) {
                return Some(path);
            }
        }
        if !dir.pop() {
            return None;
        }
    }
}

fn read_gitdir_file(path: &std::path::Path) -> Option<std::path::PathBuf> {
    let contents = std::fs::read_to_string(path).ok()?;
    let raw = contents.strip_prefix("gitdir:")?.trim();
    if raw.is_empty() {
        return None;
    }
    let gitdir = std::path::PathBuf::from(raw);
    let resolved = if gitdir.is_absolute() {
        gitdir
    } else {
        path.parent()?.join(gitdir)
    };
    if !resolved.is_dir() {
        return None;
    }
    Some(resolved.canonicalize().unwrap_or(resolved))
}

fn find_common_git_dir(git_dir: &std::path::Path) -> std::path::PathBuf {
    let commondir = git_dir.join("commondir");
    if let Ok(contents) = std::fs::read_to_string(&commondir) {
        let raw = contents.trim();
        if !raw.is_empty() {
            let common = std::path::PathBuf::from(raw);
            let resolved = if common.is_absolute() {
                common
            } else {
                git_dir.join(common)
            };
            if resolved.is_dir() {
                return resolved.canonicalize().unwrap_or(resolved);
            }
        }
    }
    git_dir.to_path_buf()
}
