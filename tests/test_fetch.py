"""Tests for the Google Drive download stage."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google import proc
from ditch_google.photos.fetch import (
    RemoteArchive,
    fetch_archive,
    list_archives,
    parse_progress,
    register_archives,
)
from ditch_google.state import ArchiveStage, State
from tests.conftest import FakeTool

#: A real rclone stats line, as emitted with --use-json-log --stats.
RCLONE_STATS_LINE = json.dumps(
    {
        "time": "2026-08-01T15:23:10.841453+02:00",
        "level": "notice",
        "msg": "Transferred: 10 MiB / 20 MiB",
        "stats": {
            "bytes": 10485760,
            "totalBytes": 20971520,
            "speed": 1048576.0,
            "eta": 10,
            "errors": 0,
            "transfers": 0,
        },
    }
)

LSJSON_OUTPUT = json.dumps(
    [
        {"Path": "takeout-002.tgz", "Name": "takeout-002.tgz", "Size": 2048, "IsDir": False},
        {"Path": "takeout-001.tgz", "Name": "takeout-001.tgz", "Size": 1024, "IsDir": False},
        {
            "Path": "archive_browser.html",
            "Name": "archive_browser.html",
            "Size": 12,
            "IsDir": False,
        },
    ]
)


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        yield opened


# -------------------------------------------------------------------------- listing


def test_list_archives_sorted_and_filtered(fake_tool: FakeTool) -> None:
    """Non-archive files sit alongside the parts; Takeout always leaves an HTML index."""
    fake_tool("rclone", stdout=LSJSON_OUTPUT)
    archives = list_archives("drive:Takeout")

    assert [a.name for a in archives] == ["takeout-001.tgz", "takeout-002.tgz"]
    assert archives[0].remote_path == "drive:Takeout/takeout-001.tgz"
    assert archives[0].size_bytes == 1024


def test_list_archives_accepts_zip_exports(fake_tool: FakeTool) -> None:
    """Takeout offers .zip or .tgz at export time; both are valid."""
    fake_tool(
        "rclone",
        stdout=json.dumps([{"Path": "t-001.zip", "Name": "t-001.zip", "Size": 1, "IsDir": False}]),
    )
    assert [a.name for a in list_archives("drive:Takeout")] == ["t-001.zip"]


def test_list_archives_empty(fake_tool: FakeTool) -> None:
    fake_tool("rclone", stdout="[]")
    assert list_archives("drive:Takeout") == []


def test_register_archives_is_idempotent(state: State) -> None:
    archives = [RemoteArchive("a.tgz", 10, "drive:Takeout/a.tgz")]
    register_archives(state, archives)
    state.set_archive_stage("a.tgz", ArchiveStage.FETCHED)
    register_archives(state, archives)

    stored = state.get_archive("a.tgz")
    assert stored is not None
    assert stored.stage is ArchiveStage.FETCHED
    assert len(state.archives()) == 1


# ------------------------------------------------------------------------- progress


def test_parse_progress_reads_rclone_stats() -> None:
    progress = parse_progress(RCLONE_STATS_LINE)
    assert progress is not None
    assert progress.bytes_done == 10485760
    assert progress.bytes_total == 20971520
    assert progress.fraction == pytest.approx(0.5)
    assert progress.eta_seconds == 10


@pytest.mark.parametrize(
    "line",
    [
        "not json at all",
        json.dumps({"level": "notice", "msg": "Config file not found"}),  # no stats
        json.dumps(["a", "list"]),
        "",
    ],
)
def test_parse_progress_ignores_lines_without_stats(line: str) -> None:
    """rclone interleaves ordinary log messages with stats; those aren't errors."""
    assert parse_progress(line) is None


def test_progress_fraction_handles_unknown_total() -> None:
    progress = parse_progress(json.dumps({"stats": {"bytes": 5, "totalBytes": 0}}))
    assert progress is not None
    assert progress.fraction == 0.0


# -------------------------------------------------------------------------- fetching


def test_fetch_archive_downloads_and_marks_fetched(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    state.add_archive("a.tgz", "drive:Takeout/a.tgz", 10)
    fake_tool("rclone", script='printf "%s\\n" \'' + RCLONE_STATS_LINE + "' >&2")

    seen: list[float] = []
    fetch_archive(state, "a.tgz", tmp_path / "work", on_progress=lambda p: seen.append(p.fraction))

    stored = state.get_archive("a.tgz")
    assert stored is not None
    assert stored.stage is ArchiveStage.FETCHED
    assert seen == [pytest.approx(0.5)]


def test_fetch_archive_reads_progress_from_stderr(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """rclone writes its JSON progress log to stderr, not stdout."""
    state.add_archive("a.tgz", "drive:Takeout/a.tgz", 10)
    fake_tool("rclone", script='printf "%s\\n" \'' + RCLONE_STATS_LINE + "' >&2")

    seen: list[int] = []
    fetch_archive(
        state, "a.tgz", tmp_path / "work", on_progress=lambda p: seen.append(p.bytes_done)
    )
    assert seen == [10485760]


def test_fetch_archive_passes_the_remote_path_to_rclone(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    state.add_archive("a.tgz", "drive:Takeout/a.tgz", 10)
    argv_log = tmp_path / "argv.txt"
    fake_tool("rclone", script=f'printf "%s\\n" "$@" > {argv_log}')

    fetch_archive(state, "a.tgz", tmp_path / "work")

    recorded = argv_log.read_text().splitlines()
    assert "copyto" in recorded
    assert "drive:Takeout/a.tgz" in recorded
    assert str(tmp_path / "work" / "a.tgz") in recorded
    assert "--use-json-log" in recorded


def test_fetch_archive_records_failure(state: State, fake_tool: FakeTool, tmp_path: Path) -> None:
    """A failed download must leave a reason in the ledger, not just a missing file."""
    state.add_archive("a.tgz", "drive:Takeout/a.tgz", 10)
    fake_tool("rclone", stderr="directory not found", exit_code=3)

    with pytest.raises(proc.ToolFailedError):
        fetch_archive(state, "a.tgz", tmp_path / "work")

    stored = state.get_archive("a.tgz")
    assert stored is not None
    assert stored.stage is ArchiveStage.FAILED
    assert stored.error is not None
    assert "directory not found" in stored.error


def test_fetch_unknown_archive_raises(state: State, fake_tool: FakeTool, tmp_path: Path) -> None:
    fake_tool("rclone")
    with pytest.raises(KeyError):
        fetch_archive(state, "never-registered.tgz", tmp_path / "work")
