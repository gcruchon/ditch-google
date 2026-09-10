"""Smoke tests for the CLI surface.

These assert the command tree exists and is wired up. Behaviour tests live alongside each
stage as it is implemented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ditch_google import __version__
from ditch_google.cli import app
from ditch_google.state import ItemStage, State

runner = CliRunner()

PHOTO_STAGES = [
    "migrate",
    "fetch",
    "unpack",
    "fix",
    "upload",
    "albums",
    "verify",
    "status",
]


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"ditch-google {__version__}\n"


def test_version_output_is_plain_text_even_when_colour_is_forced() -> None:
    """`--version` is parsed by scripts, so it must never carry ANSI escapes.

    Regression: printing it via `rich` syntax-highlighted the version number, which
    injected escape codes whenever colour was forced - as it is on CI via FORCE_COLOR.
    """
    result = runner.invoke(app, ["--version"], env={"FORCE_COLOR": "1", "TERM": "xterm-256color"})
    assert result.exit_code == 0
    assert "\x1b[" not in result.stdout
    assert result.stdout == f"ditch-google {__version__}\n"


def test_bare_invocation_shows_help() -> None:
    """`no_args_is_help` prints help and exits 2, per Click convention."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "photos" in result.stdout
    assert "doctor" in result.stdout


def test_photos_help_lists_every_stage() -> None:
    result = runner.invoke(app, ["photos", "--help"])
    assert result.exit_code == 0
    for stage in PHOTO_STAGES:
        assert stage in result.stdout


@pytest.mark.parametrize("stage", PHOTO_STAGES)
def test_every_stage_has_help(stage: str) -> None:
    """Every declared stage is built and documents itself."""
    result = runner.invoke(app, ["photos", stage, "--help"])
    assert result.exit_code == 0


def test_doctor_passes_when_all_tools_are_healthy(all_tools_healthy: None, tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--work-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "proton-drive" in result.stdout


def test_doctor_exits_nonzero_when_a_tool_is_missing(fake_bin: Path, tmp_path: Path) -> None:
    """doctor is meant to be usable as a gate in a script, so failure must be an exit code."""
    result = runner.invoke(app, ["doctor", "--work-dir", str(tmp_path)])
    assert result.exit_code == 1


def test_doctor_shows_the_remedy_for_a_failure(fake_bin: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--work-dir", str(tmp_path)])
    assert "rclone.org/install" in result.stdout


def test_doctor_json_output_is_machine_readable(all_tools_healthy: None, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["doctor", "--work-dir", str(tmp_path), "--json"],
        env={"FORCE_COLOR": "1", "TERM": "xterm-256color"},
    )
    assert result.exit_code == 0
    assert "\x1b[" not in result.stdout

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert {c["name"] for c in payload["checks"]} >= {"rclone", "exiftool", "proton-drive"}


def test_doctor_json_reports_not_ok_on_failure(fake_bin: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--work-dir", str(tmp_path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False


def test_unknown_command_is_an_error() -> None:
    result = runner.invoke(app, ["photos", "teleport"])
    assert result.exit_code != 0


def test_status_before_any_migration_has_started(tmp_path: Path) -> None:
    """A first-time user running `status` should get an explanation, not a stack trace."""
    result = runner.invoke(app, ["photos", "status", "--state-db", str(tmp_path / "none.db")])
    assert result.exit_code == 0
    assert "No migration has been started" in result.stdout


def test_status_json_before_any_migration(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["photos", "status", "--state-db", str(tmp_path / "none.db"), "--json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["started"] is False


def test_status_reports_progress(tmp_path: Path) -> None:
    db = tmp_path / "photos.db"
    with State.open(db) as state:
        state.add_archive("a.tgz", "drive:Takeout/a.tgz")
        state.add_item("a.tgz", "/work/done.jpg")
        state.add_item("a.tgz", "/work/todo.jpg")
        state.set_item_stage("/work/done.jpg", ItemStage.VERIFIED)

    result = runner.invoke(app, ["photos", "status", "--state-db", str(db), "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["started"] is True
    assert payload["complete"] is False
    assert payload["items"]["verified"] == 1
    assert payload["items"]["discovered"] == 1
    assert payload["archives"]["pending"] == 1


def test_status_reports_unmatched_sidecars(tmp_path: Path) -> None:
    """Unmatched media is the number a user most needs to see."""
    db = tmp_path / "photos.db"
    with State.open(db) as state:
        state.add_archive("a.tgz", "remote:a.tgz")
        state.add_item("a.tgz", "/work/orphan.jpg")

    result = runner.invoke(app, ["photos", "status", "--state-db", str(db)])
    assert "1 item(s) have no matching sidecar" in result.stdout


def test_status_json_is_plain_text_when_colour_is_forced(tmp_path: Path) -> None:
    db = tmp_path / "photos.db"
    with State.open(db) as state:
        state.add_archive("a.tgz", "remote:a.tgz")

    result = runner.invoke(
        app,
        ["photos", "status", "--state-db", str(db), "--json"],
        env={"FORCE_COLOR": "1", "TERM": "xterm-256color"},
    )
    assert "\x1b[" not in result.stdout
    json.loads(result.stdout)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (2048, "2.0 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2048 * 1024**3, "2048.0 GB"),
    ],
)
def test_human_bytes_uses_a_sensible_scale(count: int, expected: str) -> None:
    """ "0.0 GB" is a useless thing to print after a small first run."""
    from ditch_google.cli import _human_bytes

    assert _human_bytes(count) == expected
