"""The exit-code map is a stable CI contract (F147 spec §5.3)."""
from __future__ import annotations

from errorta_cli import errors


def test_exit_codes_are_the_documented_values() -> None:
    assert errors.CliError.exit_code == 1
    assert errors.LockBusy.exit_code == 3
    assert errors.ResidencyRefused.exit_code == 4
    assert errors.AlphaLocked.exit_code == 5
    assert errors.OriginDenied.exit_code == 6
    assert errors.RunFailed.exit_code == 7
    assert errors.NotFound.exit_code == 8
    assert errors.SidecarUnreachable.exit_code == 9
    assert errors.ForeignSidecar.exit_code == 10


def test_module_constants_match_classes() -> None:
    assert errors.EXIT_OK == 0
    assert errors.EXIT_LOCK_BUSY == errors.LockBusy.exit_code
    assert errors.EXIT_RESIDENCY == errors.ResidencyRefused.exit_code
    assert errors.EXIT_ALPHA_LOCKED == errors.AlphaLocked.exit_code
    assert errors.EXIT_ORIGIN_DENIED == errors.OriginDenied.exit_code
    assert errors.EXIT_RUN_FAILED == errors.RunFailed.exit_code
    assert errors.EXIT_NOT_FOUND == errors.NotFound.exit_code
    assert errors.EXIT_SIDECAR_UNREACHABLE == errors.SidecarUnreachable.exit_code
    assert errors.EXIT_FOREIGN_SIDECAR == errors.ForeignSidecar.exit_code


def test_leaf_exit_codes_are_unique() -> None:
    leaves = [c for c in errors.ERROR_CLASSES if c is not errors.CliError]
    codes = [c.exit_code for c in leaves]
    assert len(codes) == len(set(codes)), "exit codes must be distinct per class"


def test_every_error_carries_message_and_optional_code() -> None:
    exc = errors.LockBusy("busy", code="lock")
    assert exc.message == "busy"
    assert exc.code == "lock"
    assert isinstance(exc, errors.CliError)


def test_all_error_classes_have_int_exit_codes() -> None:
    for cls in errors.ERROR_CLASSES:
        assert isinstance(cls.exit_code, int)
