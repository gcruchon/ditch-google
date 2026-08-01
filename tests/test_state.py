"""Tests for the resumable ledger.

The behaviour that matters most here is idempotency. A resumed run re-walks archives it
has already seen, and every one of those re-walks must be a no-op rather than a duplicate
upload or a reset stage.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google.state import (
    ArchiveStage,
    ItemStage,
    State,
    default_state_path,
)


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        yield opened


# ------------------------------------------------------------------------- lifecycle


def test_open_creates_the_database_and_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "photos.db"
    with State.open(target):
        pass
    assert target.exists()


def test_default_state_path_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_state_path() == tmp_path / "state" / "ditch-google" / "photos.db"


def test_reopening_preserves_data(tmp_path: Path) -> None:
    target = tmp_path / "photos.db"
    with State.open(target) as first:
        first.add_archive("a.tgz", "drive:Takeout/a.tgz")
        first.set_archive_stage("a.tgz", ArchiveStage.FETCHED)

    with State.open(target) as second:
        archive = second.get_archive("a.tgz")
        assert archive is not None
        assert archive.stage is ArchiveStage.FETCHED


def test_migrations_are_not_reapplied(tmp_path: Path) -> None:
    """Re-opening must not re-run CREATE TABLE, and must not wipe existing rows."""
    target = tmp_path / "photos.db"
    with State.open(target) as first:
        first.add_archive("a.tgz", "remote:a.tgz")

    with State.open(target) as second:
        assert len(second.archives()) == 1


def test_user_version_matches_the_number_of_migrations(tmp_path: Path) -> None:
    """The version and the schema commit together, so they can never disagree."""
    from ditch_google.state import _MIGRATIONS

    with State.open(tmp_path / "photos.db") as opened:
        version = opened._db.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(_MIGRATIONS)


def test_wal_mode_is_enabled(tmp_path: Path) -> None:
    """WAL is what lets `status` read the ledger while a migration is writing to it."""
    with State.open(tmp_path / "photos.db") as opened:
        mode = opened._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_are_enforced(state: State) -> None:
    """An item must belong to a known archive, or the ledger can't be reconciled."""
    with pytest.raises(sqlite3.IntegrityError):
        state.add_item("never-registered.tgz", "/work/a.jpg")


# -------------------------------------------------------------------------- archives


def test_add_archive_is_idempotent_and_preserves_progress(state: State) -> None:
    """Re-discovering an archive mid-run must not send it back to `pending`."""
    state.add_archive("a.tgz", "drive:Takeout/a.tgz", size_bytes=100)
    state.set_archive_stage("a.tgz", ArchiveStage.UNPACKED)

    state.add_archive("a.tgz", "drive:Takeout/a.tgz", size_bytes=100)

    archive = state.get_archive("a.tgz")
    assert archive is not None
    assert archive.stage is ArchiveStage.UNPACKED
    assert len(state.archives()) == 1


def test_add_archive_keeps_a_known_size_when_rediscovered_without_one(state: State) -> None:
    state.add_archive("a.tgz", "remote:a.tgz", size_bytes=4096)
    state.add_archive("a.tgz", "remote:a.tgz")

    archive = state.get_archive("a.tgz")
    assert archive is not None
    assert archive.size_bytes == 4096


def test_next_archive_returns_them_in_order(state: State) -> None:
    state.add_archive("b.tgz", "remote:b.tgz")
    state.add_archive("a.tgz", "remote:a.tgz")

    first = state.next_archive()
    assert first is not None
    assert first.name == "a.tgz"


def test_next_archive_skips_completed(state: State) -> None:
    state.add_archive("a.tgz", "remote:a.tgz")
    state.add_archive("b.tgz", "remote:b.tgz")
    state.set_archive_stage("a.tgz", ArchiveStage.COMPLETED)

    remaining = state.next_archive()
    assert remaining is not None
    assert remaining.name == "b.tgz"


def test_next_archive_does_not_retry_failures(state: State) -> None:
    """A failure would recur on retry; hiding it behind a loop helps nobody."""
    state.add_archive("a.tgz", "remote:a.tgz")
    state.set_archive_stage("a.tgz", ArchiveStage.FAILED, error="checksum mismatch")

    assert state.next_archive() is None
    archive = state.get_archive("a.tgz")
    assert archive is not None
    assert archive.error == "checksum mismatch"


def test_next_archive_is_none_when_everything_is_done(state: State) -> None:
    state.add_archive("a.tgz", "remote:a.tgz")
    state.set_archive_stage("a.tgz", ArchiveStage.COMPLETED)
    assert state.next_archive() is None


# ----------------------------------------------------------------------------- items


@pytest.fixture
def archive(state: State) -> str:
    state.add_archive("a.tgz", "remote:a.tgz")
    return "a.tgz"


def test_add_item_returns_a_stable_id(state: State, archive: str) -> None:
    first = state.add_item(archive, "/work/IMG_1.jpg")
    second = state.add_item(archive, "/work/IMG_1.jpg")
    assert first == second
    assert len(state.items()) == 1


def test_add_item_does_not_reset_progress(state: State, archive: str) -> None:
    """The resume path re-walks unpacked archives; that must not undo an upload."""
    state.add_item(archive, "/work/IMG_1.jpg")
    state.set_item_stage("/work/IMG_1.jpg", ItemStage.UPLOADED, proton_uid="node-1")

    state.add_item(archive, "/work/IMG_1.jpg")

    item = state.get_item("/work/IMG_1.jpg")
    assert item is not None
    assert item.stage is ItemStage.UPLOADED
    assert item.proton_uid == "node-1"


def test_add_item_can_attach_a_sidecar_later(state: State, archive: str) -> None:
    state.add_item(archive, "/work/IMG_1.jpg")
    state.add_item(archive, "/work/IMG_1.jpg", "/work/IMG_1.jpg.json")

    item = state.get_item("/work/IMG_1.jpg")
    assert item is not None
    assert item.sidecar_path == "/work/IMG_1.jpg.json"


def test_add_items_bulk_is_idempotent(state: State, archive: str) -> None:
    entries = [(f"/work/IMG_{n}.jpg", f"/work/IMG_{n}.jpg.json") for n in range(50)]
    state.add_items(archive, entries)
    state.add_items(archive, entries)
    assert len(state.items()) == 50


def test_set_item_stage_preserves_uid_and_sha1_across_later_stages(
    state: State, archive: str
) -> None:
    """Verification must not wipe the identity recorded at upload."""
    state.add_item(archive, "/work/IMG_1.jpg")
    state.set_item_stage("/work/IMG_1.jpg", ItemStage.UPLOADED, proton_uid="node-1", sha1="abc")
    state.set_item_stage("/work/IMG_1.jpg", ItemStage.VERIFIED)

    item = state.get_item("/work/IMG_1.jpg")
    assert item is not None
    assert item.stage is ItemStage.VERIFIED
    assert item.proton_uid == "node-1"
    assert item.sha1 == "abc"


def test_failed_item_keeps_its_error(state: State, archive: str) -> None:
    state.add_item(archive, "/work/IMG_1.jpg")
    state.set_item_stage("/work/IMG_1.jpg", ItemStage.FAILED, error="upload rejected")

    item = state.get_item("/work/IMG_1.jpg")
    assert item is not None
    assert item.error == "upload rejected"


def test_pending_items_excludes_terminal_stages(state: State, archive: str) -> None:
    state.add_item(archive, "/work/done.jpg")
    state.add_item(archive, "/work/failed.jpg")
    state.add_item(archive, "/work/skipped.jpg")
    state.add_item(archive, "/work/todo.jpg")
    state.set_item_stage("/work/done.jpg", ItemStage.VERIFIED)
    state.set_item_stage("/work/failed.jpg", ItemStage.FAILED)
    state.set_item_stage("/work/skipped.jpg", ItemStage.SKIPPED)

    pending = state.pending_items()
    assert [item.source_path for item in pending] == ["/work/todo.jpg"]


def test_items_can_be_filtered_by_stage_and_archive(state: State) -> None:
    state.add_archive("a.tgz", "remote:a.tgz")
    state.add_archive("b.tgz", "remote:b.tgz")
    state.add_item("a.tgz", "/work/a1.jpg")
    state.add_item("b.tgz", "/work/b1.jpg")
    state.set_item_stage("/work/a1.jpg", ItemStage.FIXED)

    assert [i.source_path for i in state.items(archive="b.tgz")] == ["/work/b1.jpg"]
    assert [i.source_path for i in state.items(stage=ItemStage.FIXED)] == ["/work/a1.jpg"]


def test_items_without_sidecar_are_reported(state: State, archive: str) -> None:
    """Unmatched media is the headline number in the final report."""
    state.add_item(archive, "/work/matched.jpg", "/work/matched.jpg.json")
    state.add_item(archive, "/work/orphan.jpg")

    assert [i.source_path for i in state.items_without_sidecar()] == ["/work/orphan.jpg"]


# ---------------------------------------------------------------------------- albums


def test_add_album_is_idempotent(state: State) -> None:
    """Otherwise a resumed run creates a second 'Holiday 2019' beside the first."""
    first = state.add_album("Holiday 2019")
    second = state.add_album("Holiday 2019")
    assert first == second
    assert len(state.albums()) == 1


def test_add_album_keeps_a_known_proton_uid(state: State) -> None:
    state.add_album("Holiday 2019", proton_uid="album-1")
    state.add_album("Holiday 2019")

    album = state.get_album("Holiday 2019")
    assert album is not None
    assert album.proton_uid == "album-1"


def test_album_membership_and_pending_filter(state: State, archive: str) -> None:
    album_id = state.add_album("Lisbon")
    added = state.add_item(archive, "/work/a.jpg")
    not_added = state.add_item(archive, "/work/b.jpg")
    state.link_item_to_album(album_id, added)
    state.link_item_to_album(album_id, not_added)
    state.mark_album_item_added(album_id, added)

    assert len(state.album_members(album_id)) == 2
    pending = state.album_members(album_id, pending_only=True)
    assert [item.source_path for item in pending] == ["/work/b.jpg"]


def test_linking_the_same_item_twice_is_harmless(state: State, archive: str) -> None:
    album_id = state.add_album("Lisbon")
    item_id = state.add_item(archive, "/work/a.jpg")
    state.link_item_to_album(album_id, item_id)
    state.link_item_to_album(album_id, item_id)

    assert len(state.album_members(album_id)) == 1


def test_deleting_an_archive_cascades(state: State, archive: str) -> None:
    state.add_item(archive, "/work/a.jpg")
    state._db.execute("DELETE FROM archives WHERE name = ?", (archive,))
    assert state.items() == []


# --------------------------------------------------------------------------- summary


def test_counts_include_stages_with_no_rows(state: State, archive: str) -> None:
    """A stable set of keys keeps `--json` output predictable for scripts."""
    state.add_item(archive, "/work/a.jpg")

    counts = state.item_counts()
    assert counts["discovered"] == 1
    assert counts["verified"] == 0
    assert set(counts) == {s.value for s in ItemStage}

    assert set(state.archive_counts()) == {s.value for s in ArchiveStage}


def test_is_complete(state: State) -> None:
    state.add_archive("a.tgz", "remote:a.tgz")
    state.add_item("a.tgz", "/work/a.jpg")
    assert not state.is_complete()

    state.set_archive_stage("a.tgz", ArchiveStage.COMPLETED)
    state.set_item_stage("/work/a.jpg", ItemStage.VERIFIED)
    assert state.is_complete()


# ----------------------------------------------------------------------- transactions


def test_transaction_rolls_back_on_error(state: State, archive: str) -> None:
    def write_then_fail() -> None:
        with state.transaction():
            state.add_item(archive, "/work/a.jpg")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        write_then_fail()

    assert state.items() == []


def test_transaction_commits_on_success(state: State, archive: str) -> None:
    with state.transaction():
        state.add_item(archive, "/work/a.jpg")

    assert len(state.items()) == 1
