"""Tests for walking an unpacked archive and recording what it holds."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google.photos.discover import discover_archive, walk_media
from ditch_google.photos.sidecar import MatchStrategy
from ditch_google.state import State


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        opened.add_archive("a.tgz", "remote:a.tgz")
        yield opened


def build(root: Path, *names: str) -> Path:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    return root


def test_walk_splits_media_from_sidecars(tmp_path: Path) -> None:
    root = build(
        tmp_path,
        "Takeout/Google Photos/IMG_1.jpg",
        "Takeout/Google Photos/IMG_1.jpg.supplemental-metadata.json",
        "Takeout/Google Photos/VID_1.mp4",
    )
    media, sidecars = walk_media(root)

    assert [p.name for p in media] == ["IMG_1.jpg", "VID_1.mp4"]
    assert [p.name for p in sidecars] == ["IMG_1.jpg.supplemental-metadata.json"]


def test_walk_ignores_non_media_files(tmp_path: Path) -> None:
    """Takeout leaves an HTML index and other clutter in the archive."""
    root = build(tmp_path, "Takeout/archive_browser.html", "Takeout/README.txt", "Takeout/a.jpg")
    media, _ = walk_media(root)
    assert [p.name for p in media] == ["a.jpg"]


def test_walk_handles_a_missing_directory(tmp_path: Path) -> None:
    media, sidecars = walk_media(tmp_path / "never-created")
    assert media == []
    assert sidecars == []


def test_discover_records_matched_and_unmatched(state: State, tmp_path: Path) -> None:
    root = build(
        tmp_path / "unpacked",
        "IMG_1.jpg",
        "IMG_1.jpg.supplemental-metadata.json",
        "orphan.jpg",
    )

    result = discover_archive(state, "a.tgz", root)

    assert result.media == 2
    assert result.matched == 1
    assert result.unmatched == 1


def test_unmatched_media_is_still_recorded(state: State, tmp_path: Path) -> None:
    """Silently dropping a photo is the one outcome this tool must never produce."""
    root = build(tmp_path / "unpacked", "orphan.jpg")
    discover_archive(state, "a.tgz", root)

    orphans = state.items_without_sidecar()
    assert [Path(item.source_path).name for item in orphans] == ["orphan.jpg"]


def test_discover_counts_strategies(state: State, tmp_path: Path) -> None:
    root = build(
        tmp_path / "unpacked",
        "IMG_1.jpg",
        "IMG_1.jpg.supplemental-metadata.json",
        "IMG_1-edited.jpg",
        "IMG_2.jpg",
        "IMG_2.jpg.json",
    )

    result = discover_archive(state, "a.tgz", root)

    assert result.by_strategy is not None
    assert result.by_strategy[MatchStrategy.SUPPLEMENTAL] == 1
    assert result.by_strategy[MatchStrategy.EDITED] == 1
    assert result.by_strategy[MatchStrategy.LEGACY] == 1


def test_discover_is_idempotent(state: State, tmp_path: Path) -> None:
    """Re-running discovery on resume must not duplicate rows."""
    root = build(tmp_path / "unpacked", "IMG_1.jpg", "IMG_1.jpg.json")

    discover_archive(state, "a.tgz", root)
    discover_archive(state, "a.tgz", root)

    assert len(state.items()) == 1


def test_discover_stores_the_sidecar_path(state: State, tmp_path: Path) -> None:
    root = build(tmp_path / "unpacked", "IMG_1.jpg", "IMG_1.jpg.supplemental-metadata.json")
    discover_archive(state, "a.tgz", root)

    item = state.items()[0]
    assert item.sidecar_path is not None
    assert item.sidecar_path.endswith("IMG_1.jpg.supplemental-metadata.json")
