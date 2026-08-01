"""Tests for the metadata-repair pass over the ledger."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google.photos.fix import fix_pending
from ditch_google.state import ItemStage, State

HAS_EXIFTOOL = shutil.which("exiftool") is not None
needs_exiftool = pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool is not installed")


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        opened.add_archive("a.tgz", "remote:a.tgz")
        yield opened


def make_jpeg(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, "JPEG")
    return path


def make_sidecar(path: Path, timestamp: str = "1560000000") -> Path:
    path.write_text(json.dumps({"photoTakenTime": {"timestamp": timestamp}}))
    return path


@needs_exiftool
def test_fix_writes_and_advances_items(state: State, tmp_path: Path) -> None:
    media = make_jpeg(tmp_path / "IMG_1.jpg")
    sidecar = make_sidecar(tmp_path / "IMG_1.jpg.json")
    state.add_item("a.tgz", str(media), str(sidecar))

    result = fix_pending(state)

    assert result.written == 1
    assert result.failed == 0
    item = state.get_item(str(media))
    assert item is not None
    assert item.stage is ItemStage.FIXED


@needs_exiftool
def test_items_without_a_sidecar_still_advance(state: State, tmp_path: Path) -> None:
    """They have no metadata to recover, but they must still reach the upload stage."""
    media = make_jpeg(tmp_path / "orphan.jpg")
    state.add_item("a.tgz", str(media))

    result = fix_pending(state)

    assert result.skipped_no_sidecar == 1
    item = state.get_item(str(media))
    assert item is not None
    assert item.stage is ItemStage.FIXED


@needs_exiftool
def test_one_failure_does_not_abandon_the_run(state: State, tmp_path: Path) -> None:
    """One unwritable photo out of a hundred thousand must not stop the migration."""
    good = make_jpeg(tmp_path / "good.jpg")
    state.add_item("a.tgz", str(good), str(make_sidecar(tmp_path / "good.jpg.json")))

    broken = tmp_path / "broken.jpg"
    broken.write_text("this is definitely not a jpeg")
    state.add_item("a.tgz", str(broken), str(make_sidecar(tmp_path / "broken.jpg.json")))

    result = fix_pending(state)

    assert result.written == 1
    assert result.failed == 1

    failed = state.get_item(str(broken))
    assert failed is not None
    assert failed.stage is ItemStage.FAILED
    assert failed.error


@needs_exiftool
def test_fix_is_resumable(state: State, tmp_path: Path) -> None:
    """A second pass finds nothing to do, rather than rewriting every file."""
    media = make_jpeg(tmp_path / "IMG_1.jpg")
    state.add_item("a.tgz", str(media), str(make_sidecar(tmp_path / "IMG_1.jpg.json")))

    assert fix_pending(state).written == 1
    assert fix_pending(state).total == 0


@needs_exiftool
def test_fix_reports_progress(state: State, tmp_path: Path) -> None:
    for n in range(3):
        media = make_jpeg(tmp_path / f"IMG_{n}.jpg")
        state.add_item("a.tgz", str(media), str(make_sidecar(tmp_path / f"IMG_{n}.jpg.json")))

    seen: list[tuple[int, int]] = []
    fix_pending(state, on_progress=lambda done, total: seen.append((done, total)))

    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_fix_with_nothing_pending_starts_no_process(state: State) -> None:
    """Cheap guard: don't pay to start exiftool when there is no work."""
    assert fix_pending(state).total == 0
