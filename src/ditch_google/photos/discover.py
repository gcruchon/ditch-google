"""Walk an unpacked archive, pair media with sidecars, and record both in the ledger.

Splitting the walk from :mod:`ditch_google.photos.sidecar` keeps the matcher a pure
function of filenames - which is what makes it cheap to test against the long tail of
Google's naming quirks without touching a filesystem.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ditch_google.photos.sidecar import MatchStrategy, match_directory
from ditch_google.state import State

__all__ = ["MEDIA_SUFFIXES", "DiscoveryResult", "discover_archive", "walk_media"]

#: Everything Google Photos will export. Anything else in the archive is not media.
MEDIA_SUFFIXES: frozenset[str] = frozenset(
    {
        # stills
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
        ".bmp",
        ".raw",
        ".dng",
        ".cr2",
        ".nef",
        ".arw",
        ".orf",
        ".rw2",
        # video
        ".mp4",
        ".mov",
        ".m4v",
        ".avi",
        ".mkv",
        ".webm",
        ".3gp",
        ".mts",
        ".mpg",
        ".mpeg",
        ".wmv",
        # motion-photo companions
        ".mp",
    }
)


@dataclass(frozen=True)
class DiscoveryResult:
    """What one archive contained."""

    media: int = 0
    matched: int = 0
    by_strategy: dict[MatchStrategy, int] | None = None

    @property
    def unmatched(self) -> int:
        return self.media - self.matched


def walk_media(root: Path) -> tuple[list[Path], list[Path]]:
    """Split every file under ``root`` into media and sidecar JSON.

    Takeout also emits album ``metadata.json`` files and a print-order JSON; those are
    picked up here as sidecars and simply never match a media file, which is harmless.
    The albums stage reads them separately.
    """
    media: list[Path] = []
    sidecars: list[Path] = []

    for path in _walk(root):
        suffix = path.suffix.lower()
        if suffix == ".json":
            sidecars.append(path)
        elif suffix in MEDIA_SUFFIXES:
            media.append(path)

    return sorted(media), sorted(sidecars)


def _walk(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def discover_archive(state: State, archive_name: str, unpacked_root: Path) -> DiscoveryResult:
    """Record every media file in an unpacked archive, with its sidecar if one matched.

    Unmatched media is still recorded, with a null sidecar, so it survives into the final
    report. Silently dropping a photo is the one outcome this tool must never produce.
    """
    media_files, sidecar_files = walk_media(unpacked_root)
    matches = match_directory(media_files, sidecar_files)

    by_strategy: dict[MatchStrategy, int] = {}
    entries: list[tuple[str, str | None]] = []
    matched = 0

    for result in matches:
        entries.append(
            (str(result.media), str(result.sidecar) if result.sidecar is not None else None)
        )
        if result.strategy is not None:
            matched += 1
            by_strategy[result.strategy] = by_strategy.get(result.strategy, 0) + 1

    state.add_items(archive_name, entries)

    return DiscoveryResult(media=len(matches), matched=matched, by_strategy=by_strategy)
