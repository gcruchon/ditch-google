"""Smoke tests for the CLI surface.

These assert the command tree exists and is wired up. Behaviour tests live alongside each
stage as it is implemented.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ditch_google import __version__
from ditch_google.cli import app

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
def test_unimplemented_stage_exits_cleanly(stage: str) -> None:
    """A declared-but-unbuilt stage must fail loudly, not silently succeed."""
    result = runner.invoke(app, ["photos", stage])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_doctor_is_registered() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 2


def test_unknown_command_is_an_error() -> None:
    result = runner.invoke(app, ["photos", "teleport"])
    assert result.exit_code != 0
