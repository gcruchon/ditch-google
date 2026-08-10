"""Tests for the streaming orchestration.

The behaviour that matters here is the streaming property itself: local files must be
gone before the next archive starts, or the whole point of the design is lost.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google.photos.pipeline import run_migration
from ditch_google.state import ArchiveStage, ItemStage, State
from tests.conftest import FakeTool

#: Resolved at import time, before any fixture replaces PATH. Looking it up later would
#: return None, because `fake_bin` has by then narrowed PATH to the stub directory.
REAL_EXIFTOOL = shutil.which("exiftool")
needs_exiftool = pytest.mark.skipif(REAL_EXIFTOOL is None, reason="exiftool is not installed")

SIDECAR = json.dumps(
    {
        "photoTakenTime": {"timestamp": "1560000000"},
        "geoData": {"latitude": 38.7071, "longitude": -9.1355, "altitude": 0.0},
    }
)

UPLOAD_SUMMARY = json.dumps(
    {
        "transferredItems": 1,
        "transferredBytes": 1000,
        "skippedItems": 0,
        "failedItems": 0,
        "failures": [],
    }
)


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        yield opened


def build_archive(remote: Path, name: str, photo: str, album: str | None = None) -> Path:
    """A realistic single-part Takeout archive containing one photo."""
    from PIL import Image

    stage = remote.parent / f"stage-{name}"
    folder = stage / "Takeout" / "Google Photos" / (album or "Photos from 2019")
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(folder / photo, "JPEG")
    (folder / f"{photo}.supplemental-metadata.json").write_text(SIDECAR)
    if album:
        (folder / "metadata.json").write_text(json.dumps({"title": album}))

    remote.mkdir(parents=True, exist_ok=True)
    archive = remote / name
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage / "Takeout", arcname="Takeout")
    return archive


@pytest.fixture(autouse=True)
def local_rclone(fake_tool: FakeTool, fake_bin: Path) -> None:
    """An rclone that really works, against a local directory as the "remote".

    Emulating the two subcommands the pipeline uses - rather than asserting on argv -
    means these tests exercise the real fetch code path, including files actually
    arriving on disk, which is what the reclaim assertions depend on.

    The real exiftool is linked in beside the stubs: `fake_bin` replaces PATH entirely,
    so without this the metadata stage would fail on tests that are marked as *requiring*
    exiftool - which is checked against the real PATH at collection time.
    """
    if REAL_EXIFTOOL is not None:
        (fake_bin / "exiftool").symlink_to(REAL_EXIFTOOL)

    fake_tool(
        "rclone",
        script=r"""
case "$1" in
  lsjson)
    printf '['
    first=1
    for f in "$2"/*; do
      [ -f "$f" ] || continue
      name=$(basename "$f")
      size=$(wc -c < "$f" | tr -d ' ')
      [ $first -eq 1 ] || printf ','
      first=0
      printf '{"Path":"%s","Name":"%s","Size":%s,"IsDir":false}' "$name" "$name" "$size"
    done
    printf ']\n'
    ;;
  copyto)
    cp "$2" "$3"
    ;;
esac
""",
    )


@pytest.fixture
def proton(fake_tool: FakeTool) -> None:
    """A proton-drive that accepts uploads and reports an empty account."""
    fake_tool(
        "proton-drive",
        script=(
            'case "$2" in\n'
            f"  upload) printf '{UPLOAD_SUMMARY}\\n' ;;\n"
            "  *) printf '[]\\n' ;;\n"
            "esac"
        ),
    )


@needs_exiftool
def test_a_full_run_end_to_end(state: State, tmp_path: Path, proton: None) -> None:
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")
    build_archive(remote, "takeout-002.tgz", "IMG_2.jpg")

    result = run_migration(
        state, source=str(remote), work_dir=tmp_path / "work", check_remote=False
    )

    assert result.errors == []
    assert result.archives_processed == 2
    assert result.archives_failed == 0
    assert result.media_found == 2
    assert result.metadata_written == 2
    assert result.uploaded == 2
    assert result.report is not None
    assert result.report.clean


@needs_exiftool
def test_local_files_are_reclaimed_between_archives(
    state: State, tmp_path: Path, proton: None
) -> None:
    """The streaming property: peak disk stays near one archive, not the whole library."""
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")
    build_archive(remote, "takeout-002.tgz", "IMG_2.jpg")
    work = tmp_path / "work"

    seen_during_run: list[int] = []

    def watch(stage: str, archive: str, detail: str) -> None:
        if stage == "upload":
            # While one archive is being uploaded, no other archive may still be staged.
            staged = list(work.glob("*.tgz"))
            seen_during_run.append(len(staged))

    run_migration(state, source=str(remote), work_dir=work, check_remote=False, hook=watch)

    assert seen_during_run == [1, 1]
    assert list(work.glob("*.tgz")) == []
    assert not (work / "unpacked").exists() or not any((work / "unpacked").iterdir())


@needs_exiftool
def test_keep_local_retains_everything(state: State, tmp_path: Path, proton: None) -> None:
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")
    work = tmp_path / "work"

    result = run_migration(
        state, source=str(remote), work_dir=work, check_remote=False, keep_local=True
    )

    assert result.bytes_reclaimed == 0
    assert (work / "takeout-001.tgz").is_file()
    assert (work / "unpacked" / "takeout-001.tgz").is_dir()


@needs_exiftool
def test_albums_survive_the_files_being_deleted(state: State, tmp_path: Path, proton: None) -> None:
    """Membership is recorded per archive; the push reads only the ledger."""
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg", album="Lisbon 2019")

    result = run_migration(
        state, source=str(remote), work_dir=tmp_path / "work", check_remote=False
    )

    assert result.albums_created >= 1
    assert state.get_album("Lisbon 2019") is not None
    assert result.photos_added_to_albums >= 1


@needs_exiftool
def test_one_bad_archive_does_not_abandon_the_rest(
    state: State, tmp_path: Path, proton: None
) -> None:
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")
    (remote / "takeout-002.tgz").write_text("this is not a valid archive")
    build_archive(remote, "takeout-003.tgz", "IMG_3.jpg")

    result = run_migration(
        state, source=str(remote), work_dir=tmp_path / "work", check_remote=False
    )

    assert result.archives_processed == 2
    assert result.archives_failed == 1
    assert result.report is not None
    assert not result.report.clean

    broken = state.get_archive("takeout-002.tgz")
    assert broken is not None
    assert broken.stage is ArchiveStage.FAILED
    assert broken.error


@needs_exiftool
def test_a_second_run_is_a_no_op(state: State, tmp_path: Path, proton: None) -> None:
    """Resume must not re-fetch, re-upload, or duplicate anything."""
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg", album="Lisbon 2019")
    work = tmp_path / "work"

    first = run_migration(state, source=str(remote), work_dir=work, check_remote=False)
    second = run_migration(state, source=str(remote), work_dir=work, check_remote=False)

    assert first.uploaded == 1
    assert second.archives_processed == 0
    assert second.uploaded == 0
    assert second.photos_added_to_albums == 0


@needs_exiftool
def test_an_archive_is_only_completed_after_upload(
    state: State, tmp_path: Path, fake_tool: FakeTool
) -> None:
    """Marking it earlier would let a resumed run skip work that never happened."""
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")
    fake_tool(
        "proton-drive",
        script=(
            'case "$2" in\n'
            '  upload) echo "network down" >&2; exit 1 ;;\n'
            "  *) printf '[]\\n' ;;\n"
            "esac"
        ),
    )

    result = run_migration(
        state, source=str(remote), work_dir=tmp_path / "work", check_remote=False
    )

    assert result.archives_failed == 1
    archive = state.get_archive("takeout-001.tgz")
    assert archive is not None
    assert archive.stage is not ArchiveStage.COMPLETED


@needs_exiftool
def test_a_failed_upload_leaves_files_on_disk(
    state: State, tmp_path: Path, fake_tool: FakeTool
) -> None:
    """Reclaiming before the photos are safely in Proton would destroy the only copy."""
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")
    work = tmp_path / "work"
    fake_tool(
        "proton-drive",
        script=(
            'case "$2" in\n'
            '  upload) echo "network down" >&2; exit 1 ;;\n'
            "  *) printf '[]\\n' ;;\n"
            "esac"
        ),
    )

    run_migration(state, source=str(remote), work_dir=work, check_remote=False)

    assert (work / "takeout-001.tgz").is_file()


def test_no_archives_at_the_source(state: State, tmp_path: Path, proton: None) -> None:
    empty = tmp_path / "remote"
    empty.mkdir()

    result = run_migration(state, source=str(empty), work_dir=tmp_path / "work", check_remote=False)

    assert result.archives_processed == 0
    assert result.report is not None


@needs_exiftool
def test_progress_hook_reports_each_stage(state: State, tmp_path: Path, proton: None) -> None:
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")

    stages: list[str] = []
    run_migration(
        state,
        source=str(remote),
        work_dir=tmp_path / "work",
        check_remote=False,
        hook=lambda stage, archive, detail: stages.append(stage),
    )

    assert stages[0] == "list"
    for expected in ("fetch", "unpack", "discover", "fix", "upload", "reclaim", "albums"):
        assert expected in stages


@needs_exiftool
def test_items_are_verified_and_the_report_is_clean(
    state: State, tmp_path: Path, fake_tool: FakeTool
) -> None:
    remote = tmp_path / "remote"
    build_archive(remote, "takeout-001.tgz", "IMG_1.jpg")
    timeline = json.dumps([{"nodeUid": "x", "captureTime": "2019-06-08T13:20:00.000Z"}])
    fake_tool(
        "proton-drive",
        script=(
            'case "$2" in\n'
            f"  upload) printf '{UPLOAD_SUMMARY}\\n' ;;\n"
            f"  timeline) printf '{timeline}\\n' ;;\n"
            "  *) printf '[]\\n' ;;\n"
            "esac"
        ),
    )

    result = run_migration(state, source=str(remote), work_dir=tmp_path / "work")

    item = state.items()[0]
    assert item.stage is ItemStage.VERIFIED
    assert result.report is not None
    assert result.report.clean
