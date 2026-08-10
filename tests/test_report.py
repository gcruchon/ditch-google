"""Tests for verification and the final report."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google.photos.report import build_report
from ditch_google.photos.verify import verify_uploads
from ditch_google.state import ArchiveStage, ItemStage, State
from tests.conftest import FakeTool


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        opened.add_archive("a.tgz", "remote:a.tgz")
        yield opened


def sidecar(tmp_path: Path, name: str, timestamp: str = "1560000000") -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"photoTakenTime": {"timestamp": timestamp}}))
    return path


#: 1560000000 as an instant. Verification reads the capture time from the ledger, not the
#: sidecar file, because the streaming pipeline deletes the staged files before it runs.
TAKEN_AT = "2019-06-08T13:20:00+00:00"


def add_uploaded(state: State, path: str, tmp_path: Path, name: str) -> None:
    """Register an item as uploaded, with its capture time recorded as `fix` would."""
    state.add_item("a.tgz", path, str(sidecar(tmp_path, name)))
    state.set_item_stage(path, ItemStage.UPLOADED, taken_at=TAKEN_AT)


# --------------------------------------------------------------------------- report


def test_a_clean_report(state: State) -> None:
    state.set_archive_stage("a.tgz", ArchiveStage.COMPLETED)
    state.add_item("a.tgz", "/w/a.jpg", "/w/a.json")
    state.set_item_stage("/w/a.jpg", ItemStage.VERIFIED)

    report = build_report(state)

    assert report.clean
    assert report.items_uploaded == 1
    assert report.items_failed == 0


def test_a_report_is_not_clean_with_failures(state: State) -> None:
    """'Done' without naming what failed invites someone to delete their Google library."""
    state.set_archive_stage("a.tgz", ArchiveStage.COMPLETED)
    state.add_item("a.tgz", "/w/bad.jpg")
    state.set_item_stage("/w/bad.jpg", ItemStage.FAILED, error="too large")

    report = build_report(state)

    assert not report.clean
    assert report.items_failed == 1
    assert ("bad.jpg", "too large") in report.failures


def test_pending_items_make_a_report_unclean(state: State) -> None:
    state.set_archive_stage("a.tgz", ArchiveStage.COMPLETED)
    state.add_item("a.tgz", "/w/todo.jpg")

    assert not build_report(state).clean


def test_unmatched_sidecars_are_counted(state: State) -> None:
    state.add_item("a.tgz", "/w/orphan.jpg")
    assert build_report(state).without_sidecar == 1


def test_incomplete_albums_make_a_report_unclean(state: State) -> None:
    state.set_archive_stage("a.tgz", ArchiveStage.COMPLETED)
    item_id = state.add_item("a.tgz", "/w/a.jpg", "/w/a.json")
    state.set_item_stage("/w/a.jpg", ItemStage.VERIFIED)
    album_id = state.add_album("Lisbon")
    state.link_item_to_album(album_id, item_id)  # linked but never marked added

    report = build_report(state)
    assert report.albums_incomplete == 1
    assert not report.clean


def test_failure_list_is_capped(state: State) -> None:
    """A catastrophic run should not print a hundred thousand lines."""
    from ditch_google.photos.report import MAX_LISTED_FAILURES

    for n in range(MAX_LISTED_FAILURES + 10):
        path = f"/w/{n}.jpg"
        state.add_item("a.tgz", path)
        state.set_item_stage(path, ItemStage.FAILED, error="boom")

    report = build_report(state)
    assert report.items_failed == MAX_LISTED_FAILURES + 10
    assert len(report.failures) == MAX_LISTED_FAILURES


def test_report_json_shape(state: State) -> None:
    payload = build_report(state).as_dict()
    assert set(payload) == {
        "migration_id",
        "started_at",
        "clean",
        "archives",
        "items",
        "albums",
        "failures",
    }


def test_migration_id_is_stable(state: State) -> None:
    assert state.migration_id == state.migration_id


def test_migration_id_survives_reopening(tmp_path: Path) -> None:
    """The id is stamped into the files, so it must not change between runs."""
    db = tmp_path / "photos.db"
    with State.open(db) as first:
        original = first.migration_id
    with State.open(db) as second:
        assert second.migration_id == original


# ---------------------------------------------------------------------------- verify


def test_verify_matches_capture_times(state: State, fake_tool: FakeTool, tmp_path: Path) -> None:
    add_uploaded(state, "/w/a.jpg", tmp_path, "a")
    fake_tool(
        "proton-drive",
        stdout=json.dumps([{"nodeUid": "x", "captureTime": "2019-06-08T13:20:00.000Z"}]),
    )

    result = verify_uploads(state)

    assert result.ok
    assert result.matched_capture_times == 1


def test_verify_reports_a_missing_photo(state: State, fake_tool: FakeTool, tmp_path: Path) -> None:
    """The failure that matters: the ledger says uploaded, the timeline disagrees."""
    add_uploaded(state, "/w/a.jpg", tmp_path, "a")
    fake_tool("proton-drive", stdout="[]")

    result = verify_uploads(state)

    assert not result.ok
    assert result.missing_capture_times == ["a.jpg"]


def test_duplicate_capture_times_are_consumed_once_each(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """Two photos taken the same second need two timeline entries, not one matched twice."""
    for name in ("a", "b"):
        add_uploaded(state, f"/w/{name}.jpg", tmp_path, name)

    fake_tool(
        "proton-drive",
        stdout=json.dumps([{"nodeUid": "x", "captureTime": "2019-06-08T13:20:00.000Z"}]),
    )

    result = verify_uploads(state)
    assert result.matched_capture_times == 1
    assert len(result.missing_capture_times) == 1


def test_unreachable_proton_is_unverified_not_failed(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """A network blip must never read as data loss."""
    add_uploaded(state, "/w/a.jpg", tmp_path, "a")
    fake_tool("proton-drive", stderr="network unreachable", exit_code=1)

    result = verify_uploads(state)

    assert not result.checked
    assert result.missing_capture_times == []


def test_verify_with_nothing_uploaded(state: State, fake_tool: FakeTool) -> None:
    fake_tool("proton-drive", exit_code=1)
    assert verify_uploads(state).expected == 0


def test_verify_promotes_matched_items_to_verified(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """Regression: items stayed at `uploaded` forever, so a finished run never read clean."""
    add_uploaded(state, "/w/a.jpg", tmp_path, "a")
    fake_tool(
        "proton-drive",
        stdout=json.dumps([{"nodeUid": "x", "captureTime": "2019-06-08T13:20:00.000Z"}]),
    )

    verify_uploads(state)

    item = state.get_item("/w/a.jpg")
    assert item is not None
    assert item.stage is ItemStage.VERIFIED
    assert state.pending_items() == []


def test_verify_closes_a_finished_archive(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    add_uploaded(state, "/w/a.jpg", tmp_path, "a")
    fake_tool(
        "proton-drive",
        stdout=json.dumps([{"nodeUid": "x", "captureTime": "2019-06-08T13:20:00.000Z"}]),
    )

    verify_uploads(state)

    archive = state.get_archive("a.tgz")
    assert archive is not None
    assert archive.stage is ArchiveStage.COMPLETED
    assert build_report(state).clean


def test_an_archive_with_pending_items_is_not_closed(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    add_uploaded(state, "/w/a.jpg", tmp_path, "a")
    state.add_item("a.tgz", "/w/todo.jpg")  # still discovered
    fake_tool(
        "proton-drive",
        stdout=json.dumps([{"nodeUid": "x", "captureTime": "2019-06-08T13:20:00.000Z"}]),
    )

    verify_uploads(state)

    archive = state.get_archive("a.tgz")
    assert archive is not None
    assert archive.stage is not ArchiveStage.COMPLETED


def test_a_failed_archive_is_not_reopened(state: State, fake_tool: FakeTool) -> None:
    state.set_archive_stage("a.tgz", ArchiveStage.FAILED, error="checksum mismatch")
    fake_tool("proton-drive", stdout="[]")

    verify_uploads(state)

    archive = state.get_archive("a.tgz")
    assert archive is not None
    assert archive.stage is ArchiveStage.FAILED
