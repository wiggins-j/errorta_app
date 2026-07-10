// F-INFRA-09 Slice 5 — rollback storage + flow tests.
//
// All tests scope the storage root to a `tempfile::TempDir` so they never
// touch the operator's real `~/.errorta/versions/` directory.

use std::fs;
use std::io::Write;
use std::path::PathBuf;

use errorta_lib::rollback;
use tempfile::TempDir;

fn make_binary(dir: &std::path::Path, name: &str, contents: &[u8]) -> PathBuf {
    let path = dir.join(name);
    let mut f = fs::File::create(&path).expect("create binary fixture");
    f.write_all(contents).expect("write binary fixture");
    path
}

#[test]
fn archive_writes_binary_and_metadata() {
    let tmp = TempDir::new().unwrap();
    let bin = make_binary(tmp.path(), "errorta-fake", b"hello-binary");

    rollback::archive_current_version_in(tmp.path(), &bin, "0.5.0").expect("archive ok");

    let listed = rollback::list_versions_in(tmp.path()).expect("list ok");
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].version, "0.5.0");
    assert!(listed[0].size_bytes > 0);
}

#[test]
fn list_versions_returns_newest_first() {
    let tmp = TempDir::new().unwrap();
    let bin = make_binary(tmp.path(), "errorta-fake", b"x");
    for v in ["0.4.0", "0.5.0", "0.6.0"] {
        rollback::archive_current_version_in(tmp.path(), &bin, v).expect("archive ok");
        // Force timestamp differentiation.
        std::thread::sleep(std::time::Duration::from_millis(1100));
    }
    let listed = rollback::list_versions_in(tmp.path()).expect("list ok");
    // newest-first ordering
    assert!(listed[0].installed_at >= listed[1].installed_at);
    assert!(listed[1].installed_at >= listed[2].installed_at);
}

#[test]
fn archive_prunes_beyond_three_versions() {
    let tmp = TempDir::new().unwrap();
    let bin = make_binary(tmp.path(), "errorta-fake", b"x");
    for v in ["0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0"] {
        rollback::archive_current_version_in(tmp.path(), &bin, v).expect("archive ok");
        std::thread::sleep(std::time::Duration::from_millis(1100));
    }
    let listed = rollback::list_versions_in(tmp.path()).expect("list ok");
    assert_eq!(listed.len(), 3, "should keep at most 3");
}

#[test]
fn write_pending_marker_is_atomic_and_readable() {
    let tmp = TempDir::new().unwrap();
    // Ensure the storage dir doesn't exist yet — write should create it.
    rollback::write_pending_marker_in(tmp.path(), "0.5.0").expect("write ok");

    let marker = rollback::read_pending_marker_in(tmp.path())
        .expect("read ok")
        .expect("marker present");
    assert_eq!(marker.version, "0.5.0");

    // No `.tmp` file should be left behind.
    for entry in fs::read_dir(tmp.path().join("versions")).expect("read dir") {
        let entry = entry.unwrap();
        let name = entry.file_name().to_string_lossy().into_owned();
        assert!(
            !name.ends_with(".tmp"),
            "unexpected temp file left behind: {name}"
        );
    }
}

#[test]
fn finalize_swaps_binary_and_clears_marker() {
    let tmp = TempDir::new().unwrap();
    let original = make_binary(tmp.path(), "errorta-current", b"ORIGINAL");
    let target_binary = make_binary(tmp.path(), "errorta-fake", b"TARGET-CONTENTS");

    rollback::archive_current_version_in(tmp.path(), &target_binary, "0.5.0").expect("archive ok");
    rollback::write_pending_marker_in(tmp.path(), "0.5.0").expect("write marker");

    let swapped = rollback::finalize_pending_rollback_in(tmp.path(), &original)
        .expect("finalize ok");
    assert_eq!(swapped, Some("0.5.0".to_string()));

    // The marker is gone.
    assert!(rollback::read_pending_marker_in(tmp.path()).unwrap().is_none());

    // The binary at `original` now matches the archived contents.
    let after = fs::read(&original).expect("read after");
    assert_eq!(after, b"TARGET-CONTENTS");
}

#[test]
fn finalize_with_no_marker_is_noop() {
    let tmp = TempDir::new().unwrap();
    let bin = make_binary(tmp.path(), "errorta-current", b"unchanged");
    let result = rollback::finalize_pending_rollback_in(tmp.path(), &bin).expect("finalize ok");
    assert!(result.is_none());
    let after = fs::read(&bin).expect("read after");
    assert_eq!(after, b"unchanged");
}

#[test]
fn finalize_preserves_marker_when_archived_binary_missing() {
    let tmp = TempDir::new().unwrap();
    let original = make_binary(tmp.path(), "errorta-current", b"original");
    rollback::write_pending_marker_in(tmp.path(), "0.9.0-bogus").expect("write");

    let err = rollback::finalize_pending_rollback_in(tmp.path(), &original).expect_err("missing");
    match err {
        rollback::RollbackError::BinaryMissing(_) => {}
        e => panic!("expected BinaryMissing, got {e:?}"),
    }
    // Marker MUST still be present so the user can retry.
    assert!(rollback::read_pending_marker_in(tmp.path()).unwrap().is_some());
    // Original binary untouched.
    let after = fs::read(&original).expect("read after");
    assert_eq!(after, b"original");
}

#[test]
fn crash_recovery_round_trip() {
    let tmp = TempDir::new().unwrap();
    rollback::write_crash_recovery_marker_in(
        tmp.path(),
        "0.5.1",
        "0.5.0",
        "sidecar timeout",
    )
    .expect("write");
    let read = rollback::read_crash_recovery_in(tmp.path())
        .expect("read ok")
        .expect("present");
    assert_eq!(read.failed_version, "0.5.1");
    assert_eq!(read.rolled_back_to, "0.5.0");
    assert_eq!(read.error, "sidecar timeout");

    rollback::dismiss_crash_recovery_in(tmp.path()).expect("dismiss ok");
    let none = rollback::read_crash_recovery_in(tmp.path()).expect("read ok");
    assert!(none.is_none());
    // Calling dismiss again is idempotent.
    rollback::dismiss_crash_recovery_in(tmp.path()).expect("dismiss idempotent");
}

#[test]
fn install_marker_age_is_recent_after_archive() {
    let tmp = TempDir::new().unwrap();
    let bin = make_binary(tmp.path(), "errorta-fake", b"x");
    rollback::archive_current_version_in(tmp.path(), &bin, "0.5.0").expect("archive");
    let marker = rollback::read_install_marker_in(tmp.path()).expect("marker");
    assert_eq!(marker.current_version, "0.5.0");
    let age = marker.age();
    assert!(age.as_secs() < 5);
}
