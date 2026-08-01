"""Tests for the preflight checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from ditch_google import doctor
from ditch_google.constants import PROTON_DRIVE_MIN_VERSION
from ditch_google.doctor import Status
from tests.conftest import (
    EXIFTOOL_VERSION_OUTPUT,
    PROTON_VERSION_OUTPUT,
    RCLONE_VERSION_OUTPUT,
    FakeTool,
)

# --------------------------------------------------------------------- version parsing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Real output from each tool.
        ("rclone v1.74.2", (1, 74, 2)),
        ("13.10", (13, 10, 0)),
        ("Proton Drive CLI cli-drive@0.7.0", (0, 7, 0)),
        # The proton-drive build name varies; the parser must key off the version, not
        # the prefix. Source builds and distro repackages use different names.
        ("Proton Drive CLI external-drive-sdkclijs@0.7.1", (0, 7, 1)),
        ("Proton Drive CLI cli-drive-arch@1.0.0", (1, 0, 0)),
        # Two-part versions are padded.
        ("exiftool 12.76", (12, 76, 0)),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int]) -> None:
    assert doctor.parse_version(text) == expected


def test_parse_version_returns_none_when_absent() -> None:
    assert doctor.parse_version("no version here") is None


def test_format_version() -> None:
    assert doctor.format_version((0, 7, 0)) == "0.7.0"


# ------------------------------------------------------------------------ tool checks


def test_rclone_ok(fake_tool: FakeTool) -> None:
    fake_tool("rclone", stdout=RCLONE_VERSION_OUTPUT)
    check = doctor.check_rclone()
    assert check.status is Status.OK
    assert "1.74.2" in check.detail


def test_exiftool_ok(fake_tool: FakeTool) -> None:
    fake_tool("exiftool", stdout=EXIFTOOL_VERSION_OUTPUT)
    assert doctor.check_exiftool().status is Status.OK


def test_proton_drive_ok(fake_tool: FakeTool) -> None:
    fake_tool("proton-drive", stdout=PROTON_VERSION_OUTPUT)
    check = doctor.check_proton_drive()
    assert check.status is Status.OK
    assert "0.7.0" in check.detail


def test_proton_drive_reads_the_cli_line_not_the_sdk_line(fake_tool: FakeTool) -> None:
    """`version` prints CLI then SDK. Reading the wrong line would report the SDK's."""
    fake_tool(
        "proton-drive",
        stdout="Proton Drive CLI cli-drive@0.7.0\nProton Drive SDK js@0.15.1",
    )
    assert "0.7.0" in doctor.check_proton_drive().detail


def test_missing_tool_fails_with_a_remedy(fake_tool: FakeTool) -> None:
    fake_tool("rclone", stdout=RCLONE_VERSION_OUTPUT)  # exiftool deliberately absent
    check = doctor.check_exiftool()
    assert check.status is Status.FAIL
    assert check.detail == "not installed"
    assert check.remedy is not None
    assert "exiftool.org" in check.remedy


def test_proton_drive_below_floor_fails(fake_tool: FakeTool) -> None:
    """0.6.x can write files to Drive but cannot reach the Photos timeline at all."""
    fake_tool("proton-drive", stdout="Proton Drive CLI cli-drive@0.6.0")
    check = doctor.check_proton_drive()
    assert check.status is Status.FAIL
    assert "0.6.0" in check.detail
    assert doctor.format_version(PROTON_DRIVE_MIN_VERSION) in check.detail
    assert check.remedy is not None
    assert "proton.me/download/drive/cli" in check.remedy


def test_unparseable_version_warns_rather_than_fails(fake_tool: FakeTool) -> None:
    """An unrecognised version scheme shouldn't block someone whose tool works."""
    fake_tool("proton-drive", stdout="Proton Drive CLI nightly-build")
    assert doctor.check_proton_drive().status is Status.WARN


def test_tool_that_errors_on_version_fails(fake_tool: FakeTool) -> None:
    fake_tool("rclone", stderr="segfault", exit_code=1)
    assert doctor.check_rclone().status is Status.FAIL


# --------------------------------------------------------------------------- auth check


def test_proton_auth_ok(fake_tool: FakeTool) -> None:
    fake_tool("proton-drive", stdout="[]")
    assert doctor.check_proton_auth().status is Status.OK


def test_proton_auth_failure_points_at_the_login_command(fake_tool: FakeTool) -> None:
    fake_tool("proton-drive", stderr="Not authenticated", exit_code=1)
    check = doctor.check_proton_auth()
    assert check.status is Status.FAIL
    assert check.remedy == "Run: proton-drive auth login"


# ------------------------------------------------------------------------ source check


def test_rclone_remote_ok(fake_tool: FakeTool) -> None:
    fake_tool("rclone", stdout="          -1 2026-08-01 12:00:00        -1 Takeout")
    assert doctor.check_rclone_remote("drive:Takeout").status is Status.OK


def test_rclone_remote_missing_fails(fake_tool: FakeTool) -> None:
    fake_tool("rclone", stderr="directory not found", exit_code=3)
    check = doctor.check_rclone_remote("drive:Takeout")
    assert check.status is Status.FAIL
    assert "drive:Takeout" in check.detail
    assert check.remedy is not None
    assert "listremotes" in check.remedy


# --------------------------------------------------------------------------- disk space


def test_disk_space_ok(tmp_path: Path) -> None:
    check = doctor.check_disk_space(tmp_path, minimum_bytes=1)
    assert check.status is Status.OK
    assert "free" in check.detail


def test_disk_space_low_warns_but_does_not_fail(tmp_path: Path) -> None:
    """Low disk is advisory: the run is resumable, so it's recoverable mid-flight."""
    check = doctor.check_disk_space(tmp_path, minimum_bytes=10**18)
    assert check.status is Status.WARN
    assert check.remedy is not None
    assert "--work-dir" in check.remedy


def test_disk_space_walks_up_to_an_existing_parent(tmp_path: Path) -> None:
    """The work directory usually doesn't exist yet on a first run."""
    check = doctor.check_disk_space(tmp_path / "not" / "created" / "yet", minimum_bytes=1)
    assert check.status is Status.OK


# ------------------------------------------------------------------------- run_checks


def test_run_checks_all_healthy(all_tools_healthy: None, tmp_path: Path) -> None:
    checks = doctor.run_checks(work_dir=tmp_path)
    assert [c.name for c in checks] == [
        "rclone",
        "exiftool",
        "proton-drive",
        "proton auth",
        "disk space",
    ]
    assert not any(c.failed for c in checks)


def test_run_checks_includes_source_when_given(all_tools_healthy: None, tmp_path: Path) -> None:
    checks = doctor.run_checks(work_dir=tmp_path, source="drive:Takeout")
    assert "takeout source" in [c.name for c in checks]


def test_run_checks_skips_auth_when_proton_drive_is_too_old(
    fake_tool: FakeTool, tmp_path: Path
) -> None:
    """Reporting 'not signed in' on top of 'CLI too old' would just be noise."""
    fake_tool("rclone", stdout=RCLONE_VERSION_OUTPUT)
    fake_tool("exiftool", stdout=EXIFTOOL_VERSION_OUTPUT)
    fake_tool("proton-drive", stdout="Proton Drive CLI cli-drive@0.6.0")

    names = [c.name for c in doctor.run_checks(work_dir=tmp_path)]
    assert "proton auth" not in names


def test_run_checks_skips_source_when_rclone_is_missing(
    fake_tool: FakeTool, tmp_path: Path
) -> None:
    fake_tool("exiftool", stdout=EXIFTOOL_VERSION_OUTPUT)
    fake_tool("proton-drive", stdout=PROTON_VERSION_OUTPUT)

    names = [c.name for c in doctor.run_checks(work_dir=tmp_path, source="drive:Takeout")]
    assert "takeout source" not in names


def test_run_checks_reports_failure_when_nothing_is_installed(
    fake_bin: Path, tmp_path: Path
) -> None:
    checks = doctor.run_checks(work_dir=tmp_path)
    assert all(c.failed for c in checks if c.name != "disk space")
