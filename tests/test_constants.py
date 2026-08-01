"""Guard the constants that encode external contracts.

These aren't arbitrary numbers - each one is a documented fact about a third-party tool or
format. A change here should be a deliberate, reviewed decision.
"""

from __future__ import annotations

from ditch_google import constants


def test_proton_drive_floor_is_the_release_that_added_photos() -> None:
    """proton-drive cli/v0.7.0 (2026-07-30) added `photo upload` and the album commands.

    Lowering this floor would let the tool run against a CLI with no photo support at all.
    See docs/adr/0002-why-proton-cli-over-rclone-protondrive.md
    """
    assert constants.PROTON_DRIVE_MIN_VERSION == (0, 7, 0)


def test_sidecar_filename_budget() -> None:
    """Google truncates sidecar JSON filenames to 51 characters.

    The matcher's truncation strategy depends on this exact value.
    """
    assert constants.SIDECAR_FILENAME_BUDGET == 51
