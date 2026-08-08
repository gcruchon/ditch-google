"""Tests for album reconstruction."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google.photos.albums import (
    discover_albums,
    marker_album_name,
    restore_albums,
)
from ditch_google.state import ItemStage, State
from tests.conftest import FakeTool


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        opened.add_archive("a.tgz", "remote:a.tgz")
        yield opened


def make_album(root: Path, title: str, *photos: str) -> Path:
    directory = root / title
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.json").write_text(json.dumps({"title": title}))
    for name in photos:
        (directory / name).write_bytes(b"x")
    return directory


def register(state: State, *paths: Path, stage: ItemStage = ItemStage.UPLOADED) -> None:
    for path in paths:
        state.add_item("a.tgz", str(path))
        state.set_item_stage(str(path), stage)


# ------------------------------------------------------------------------- discovery


def test_discover_reads_the_title_from_metadata(tmp_path: Path) -> None:
    make_album(tmp_path, "Lisbon 2019", "IMG_1.jpg", "IMG_2.jpg")
    albums = discover_albums([tmp_path])

    assert [a.title for a in albums] == ["Lisbon 2019"]
    assert [p.name for p in albums[0].media] == ["IMG_1.jpg", "IMG_2.jpg"]


def test_year_folders_are_not_albums(tmp_path: Path) -> None:
    """`Photos from 2019` has the same shape but is just how Google buckets the timeline."""
    make_album(tmp_path, "Photos from 2019", "IMG_1.jpg")
    make_album(tmp_path, "Lisbon 2019", "IMG_2.jpg")

    assert [a.title for a in discover_albums([tmp_path])] == ["Lisbon 2019"]


def test_a_directory_without_metadata_is_not_an_album(tmp_path: Path) -> None:
    (tmp_path / "loose").mkdir()
    (tmp_path / "loose" / "IMG_1.jpg").write_bytes(b"x")
    assert discover_albums([tmp_path]) == []


def test_albums_split_across_archives_are_merged(tmp_path: Path) -> None:
    """Takeout scatters one album's photos across parts - this is why albums run last."""
    make_album(tmp_path / "part1", "Lisbon", "IMG_1.jpg")
    make_album(tmp_path / "part2", "Lisbon", "IMG_2.jpg")

    albums = discover_albums([tmp_path / "part1", tmp_path / "part2"])

    assert len(albums) == 1
    assert sorted(p.name for p in albums[0].media) == ["IMG_1.jpg", "IMG_2.jpg"]


def test_non_media_files_are_not_album_members(tmp_path: Path) -> None:
    directory = make_album(tmp_path, "Lisbon", "IMG_1.jpg")
    (directory / "notes.txt").write_text("hello")

    assert [p.name for p in discover_albums([tmp_path])[0].media] == ["IMG_1.jpg"]


def test_malformed_metadata_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "Broken"
    directory.mkdir()
    (directory / "metadata.json").write_text("{ not json")
    assert discover_albums([tmp_path]) == []


def test_missing_root_is_not_an_error(tmp_path: Path) -> None:
    assert discover_albums([tmp_path / "never-created"]) == []


# ---------------------------------------------------------------------- marker album


def test_marker_album_name_includes_the_date_and_id() -> None:
    name = marker_album_name("abc123def456", "2026-08-08T12:00:00+00:00")
    assert name == "Imported from Google Photos - 2026-08-08 (abc123def456)"


def test_marker_album_is_created_and_populated(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """Proton has no upload-batch concept, so the run needs a handle of its own."""
    photo = tmp_path / "IMG_1.jpg"
    photo.write_bytes(b"x")
    register(state, photo)
    fake_tool("proton-drive", stdout="[]")

    result = restore_albums(state, [tmp_path])

    expected = marker_album_name(state.migration_id, state.started_at)
    assert state.get_album(expected) is not None
    assert result.photos_added == 1


def test_marker_can_be_disabled(state: State, fake_tool: FakeTool, tmp_path: Path) -> None:
    photo = tmp_path / "IMG_1.jpg"
    photo.write_bytes(b"x")
    register(state, photo)
    fake_tool("proton-drive", stdout="[]")

    restore_albums(state, [tmp_path], marker=False)
    assert state.albums() == []


def test_marker_only_includes_uploaded_items(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    uploaded = tmp_path / "up.jpg"
    failed = tmp_path / "bad.jpg"
    uploaded.write_bytes(b"x")
    failed.write_bytes(b"x")
    register(state, uploaded)
    register(state, failed, stage=ItemStage.FAILED)
    fake_tool("proton-drive", stdout="[]")

    restore_albums(state, [tmp_path])

    album = state.get_album(marker_album_name(state.migration_id, state.started_at))
    assert album is not None
    assert [Path(i.source_path).name for i in state.album_members(album.id)] == ["up.jpg"]


# ------------------------------------------------------------------------ restoration


def test_albums_are_created_and_photos_added(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    directory = make_album(tmp_path, "Lisbon", "IMG_1.jpg", "IMG_2.jpg")
    register(state, directory / "IMG_1.jpg", directory / "IMG_2.jpg")
    fake_tool("proton-drive", stdout="[]")

    result = restore_albums(state, [tmp_path], marker=False)

    assert result.created == 1
    assert result.photos_added == 2


def test_an_existing_album_is_reused_not_duplicated(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """A resumed run must not create a second 'Lisbon' beside the first."""
    directory = make_album(tmp_path, "Lisbon", "IMG_1.jpg")
    register(state, directory / "IMG_1.jpg")
    fake_tool("proton-drive", stdout=json.dumps([{"name": "Lisbon"}]))

    result = restore_albums(state, [tmp_path], marker=False)

    assert result.created == 0
    assert result.reused == 1


def test_the_cli_wrapped_name_shape_is_understood(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """The CLI returns some fields as {"ok": true, "value": ...}."""
    directory = make_album(tmp_path, "Lisbon", "IMG_1.jpg")
    register(state, directory / "IMG_1.jpg")
    fake_tool("proton-drive", stdout=json.dumps([{"name": {"ok": True, "value": "Lisbon"}}]))

    assert restore_albums(state, [tmp_path], marker=False).reused == 1


def test_photos_are_not_added_twice_on_resume(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """Adding a photo twice is the visible, annoying failure mode of a resumed run."""
    directory = make_album(tmp_path, "Lisbon", "IMG_1.jpg")
    register(state, directory / "IMG_1.jpg")
    fake_tool("proton-drive", stdout="[]")

    first = restore_albums(state, [tmp_path], marker=False)
    second = restore_albums(state, [tmp_path], marker=False)

    assert first.photos_added == 1
    assert second.photos_added == 0


def test_a_failed_add_is_recorded_and_retried_next_run(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    directory = make_album(tmp_path, "Lisbon", "IMG_1.jpg")
    register(state, directory / "IMG_1.jpg")
    # album list succeeds (prints []), add-photo fails.
    fake_tool(
        "proton-drive",
        script='if [ "$2" = "add-photo" ]; then echo "boom" >&2; exit 1; fi\nprintf "[]\\n"',
    )

    result = restore_albums(state, [tmp_path], marker=False)
    assert result.failed
    assert result.photos_added == 0

    album = state.get_album("Lisbon")
    assert album is not None
    assert len(state.album_members(album.id, pending_only=True)) == 1


def test_a_failed_create_skips_its_photos(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    directory = make_album(tmp_path, "Lisbon", "IMG_1.jpg")
    register(state, directory / "IMG_1.jpg")
    fake_tool(
        "proton-drive",
        script='if [ "$2" = "create" ]; then echo "boom" >&2; exit 1; fi\nprintf "[]\\n"',
    )

    result = restore_albums(state, [tmp_path], marker=False)
    assert result.created == 0
    assert result.photos_added == 0
    assert result.failed


def test_photos_not_in_the_ledger_are_ignored(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """An album can reference a photo that never made it through the pipeline."""
    make_album(tmp_path, "Lisbon", "IMG_1.jpg")
    fake_tool("proton-drive", stdout="[]")

    assert restore_albums(state, [tmp_path], marker=False).photos_added == 0
