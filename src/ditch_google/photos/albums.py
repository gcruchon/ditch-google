"""Recreate Takeout albums in Proton Photos.

Takeout represents an album as a directory holding copies of its photos plus a
``metadata.json`` giving the album's title. Year folders (``Photos from 2019``) use the
same shape but are not albums - they are just how Google buckets the timeline - so they
are excluded by name.

This runs as a **final pass**, after every archive has been uploaded. Takeout scatters one
album's photos across several parts, so an album is only complete once the whole export
has been through the pipeline.

Photos are addressed by virtual path (``/photos/IMG_1234.jpg``) rather than node UID,
because ``photo upload`` never tells us the UIDs it created. That is fine - ``album
add-photo`` takes paths - but it does mean album membership is matched on filename.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ditch_google import protondrive
from ditch_google.photos.discover import MEDIA_SUFFIXES
from ditch_google.proc import ToolError
from ditch_google.state import State

__all__ = [
    "AlbumResult",
    "TakeoutAlbum",
    "discover_albums",
    "marker_album_name",
    "restore_albums",
]

#: Google's per-year timeline folders, which look like albums but are not.
_YEAR_FOLDER = re.compile(r"^Photos from \d{4}$", re.IGNORECASE)

#: How many photos to add to an album per CLI call.
ALBUM_BATCH_SIZE = 50


@dataclass(frozen=True)
class TakeoutAlbum:
    """One album as Takeout represents it."""

    title: str
    directory: Path
    media: tuple[Path, ...] = field(default_factory=tuple)


@dataclass
class AlbumResult:
    """Outcome of the album pass."""

    created: int = 0
    reused: int = 0
    photos_added: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def albums(self) -> int:
        return self.created + self.reused


def marker_album_name(migration_id: str, started_at: str | None = None) -> str:
    """Name of the album that groups everything one migration uploaded.

    Proton has no notion of an upload batch, and the only server-side trace of one is each
    photo's ``creationTime`` - which the bulk timeline listing does not even return. A
    marker album gives the run a first-class, visible handle: somewhere to look, and
    something to delete, without needing this tool or its database.

    Includes the time, not just the date, so two runs on the same day are told apart by
    something a person can read rather than only by the opaque id.

    ``started_at`` must be a **fixed** timestamp recorded once for the migration - see
    :attr:`State.started_at_local`. Every resume recomputes this name, so anything
    derived from the current clock would produce a second marker album.
    """
    stamp = (started_at or "")[:16].replace("T", " ")
    if len(stamp) < 16:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    return f"Imported from Google Photos - {stamp} ({migration_id})"


def _read_title(directory: Path) -> str | None:
    """Read an album's title from its ``metadata.json``, if it has one."""
    metadata = directory / "metadata.json"
    if not metadata.is_file():
        return None
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def discover_albums(roots: Iterable[Path]) -> list[TakeoutAlbum]:
    """Find every album across the unpacked archives.

    An album appearing in several archives is merged, since Takeout splits one album's
    photos across parts.
    """
    merged: dict[str, list[Path]] = {}
    directories: dict[str, Path] = {}

    for root in roots:
        if not root.exists():
            continue
        for directory in sorted(p for p in root.rglob("*") if p.is_dir()):
            if _YEAR_FOLDER.match(directory.name):
                continue
            title = _read_title(directory)
            if title is None:
                continue

            media = [
                path
                for path in sorted(directory.iterdir())
                if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
            ]
            merged.setdefault(title, []).extend(media)
            directories.setdefault(title, directory)

    return [
        TakeoutAlbum(title=title, directory=directories[title], media=tuple(media))
        for title, media in sorted(merged.items())
    ]


def _existing_titles() -> set[str]:
    """Titles already in Proton, so a resumed run reuses rather than duplicates."""
    titles: set[str] = set()
    for entry in protondrive.list_albums():
        name = entry.get("name")
        if isinstance(name, dict):  # the CLI wraps some fields as {ok, value}
            name = name.get("value")
        if isinstance(name, str):
            titles.add(name)
    return titles


def _ensure_album(state: State, title: str, existing: set[str], result: AlbumResult) -> bool:
    """Create the album unless it is already there. Returns whether it exists afterwards."""
    state.add_album(title)

    if title in existing:
        result.reused += 1
        return True
    try:
        protondrive.create_album(title)
    except ToolError as exc:
        result.failed.append((title, str(exc)))
        return False

    existing.add(title)
    result.created += 1
    return True


def restore_albums(
    state: State,
    roots: Iterable[Path],
    *,
    marker: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> AlbumResult:
    """Recreate every Takeout album in Proton and populate it.

    Idempotent by design: albums already present are reused rather than duplicated, and
    membership already recorded in the ledger is not re-sent. Adding a photo twice would
    otherwise be the visible, annoying failure of a resumed run.
    """
    albums = list(discover_albums(roots))
    result = AlbumResult()
    existing = _existing_titles()

    marker_title = marker_album_name(state.migration_id, state.started_at_local) if marker else None
    total = len(albums) + (1 if marker_title else 0)

    for index, album in enumerate(albums, start=1):
        if _ensure_album(state, album.title, existing, result):
            _add_media(state, album.title, album.media, result)
        if on_progress is not None:
            on_progress(index, total)

    if marker_title is not None:
        # Everything this migration uploaded, in one place.
        uploaded = [
            Path(item.source_path)
            for item in state.items()
            if item.stage.value in {"uploaded", "verified"}
        ]
        if uploaded and _ensure_album(state, marker_title, existing, result):
            _add_media(state, marker_title, uploaded, result)
        if on_progress is not None:
            on_progress(total, total)

    return result


def _add_media(state: State, title: str, media: Iterable[Path], result: AlbumResult) -> None:
    """Add photos to an album, sending only those not already recorded as added.

    Membership is recorded first, then the ledger itself decides what still needs
    sending. That keeps a resumed run from adding the same photo twice, and lets a
    previously failed batch be retried without re-sending its successful neighbours.
    """
    album_id = state.add_album(title)

    for path in media:
        item = state.get_item(str(path))
        if item is not None:
            state.link_item_to_album(album_id, item.id)

    pending = state.album_members(album_id, pending_only=True)

    for start in range(0, len(pending), ALBUM_BATCH_SIZE):
        batch = pending[start : start + ALBUM_BATCH_SIZE]
        photo_paths = [protondrive.photo_path(Path(item.source_path).name) for item in batch]
        try:
            protondrive.add_photos_to_album(protondrive.album_path(title), photo_paths)
        except ToolError as exc:
            result.failed.append((title, str(exc)))
            continue

        for item in batch:
            state.mark_album_item_added(album_id, item.id)
        result.photos_added += len(batch)
