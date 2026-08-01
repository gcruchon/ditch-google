"""Pair each media file with its Takeout sidecar JSON.

This is the hardest part of the migration and the reason the tool exists. Google Takeout
strips the capture time and GPS out of the media files and puts them in sidecar JSON
files - but names those sidecars inconsistently, with rules it has changed over the years
and has never documented.

**Matching only works in one direction.** Given a media filename you can derive the
sidecar name Google would have produced; you cannot reliably go back the other way,
because the derivation is lossy (truncation discards characters). So every strategy here
generates *candidate sidecar names from the media name* and looks them up, never the
reverse.

The naming rules, in the order they are tried:

Modern (2024+)
    ``IMG_1234.jpg`` -> ``IMG_1234.jpg.supplemental-metadata.json``
Legacy
    ``IMG_1234.jpg`` -> ``IMG_1234.jpg.json``
Stem only
    ``IMG_1234.jpg`` -> ``IMG_1234.json``
Truncated
    a long name -> clipped to 46 characters, then ``.json``
Duplicate
    ``IMG_1234(1).jpg`` -> ``IMG_1234.jpg.supplemental-metadata(1).json``
Edited
    ``IMG_1234-edited.jpg`` -> shares ``IMG_1234.jpg``'s sidecar
Live photo
    ``IMG_1234.MP4`` -> shares ``IMG_1234.HEIC``'s sidecar

The truncation rule deserves spelling out, because it produces filenames that look like
bugs. Google clips ``<media filename>.supplemental-metadata`` to 46 characters and *then*
appends ``.json``, which is never clipped. A 45-character media filename therefore yields
``<45 chars>..json`` - the 46th character is the dot that began ``.supplemental-metadata``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath

from ditch_google.constants import SIDECAR_FILENAME_BUDGET

__all__ = [
    "EDITED_SUFFIXES",
    "MatchStrategy",
    "SidecarMatch",
    "candidate_sidecar_names",
    "match_directory",
    "match_media",
]

#: The suffix Google appends to sidecars in recent exports.
SUPPLEMENTAL = ".supplemental-metadata"

#: Google localises the "edited" marker to the account's language. An edited copy has no
#: sidecar of its own and shares the original's. This list is necessarily incomplete -
#: additions from real exports are welcome, see CONTRIBUTING.md.
EDITED_SUFFIXES: tuple[str, ...] = (
    "-edited",  # English
    "-bearbeitet",  # German
    "-modifié",  # French
    "-editado",  # Spanish / Portuguese
    "-modificato",  # Italian
    "-bewerkt",  # Dutch
    "-redigerad",  # Swedish
    "-muokattu",  # Finnish
    "-edytowane",  # Polish
    "-editat",  # Romanian / Catalan
    "-düzenlendi",  # Turkish
    "-편집됨",  # Korean
)

#: Extensions Google pairs with a still image rather than giving them their own sidecar.
MOTION_SUFFIXES: tuple[str, ...] = (".mp", ".mp4", ".mov")

#: Still-image extensions a motion companion might belong to.
STILL_SUFFIXES: tuple[str, ...] = (".heic", ".jpg", ".jpeg", ".png")

_DUPLICATE_RE = re.compile(r"^(?P<stem>.*?)(?P<marker>\((?P<index>\d+)\))(?P<suffix>\.[^.]*)?$")


class MatchStrategy(StrEnum):
    """How a media file was paired with its sidecar. Recorded for the final report."""

    SUPPLEMENTAL = "supplemental"
    LEGACY = "legacy"
    STEM = "stem"
    TRUNCATED = "truncated"
    DUPLICATE = "duplicate"
    EDITED = "edited"
    LIVE_PHOTO = "live-photo"


@dataclass(frozen=True)
class SidecarMatch:
    """One media file and the sidecar it was paired with."""

    media: Path
    sidecar: Path | None
    strategy: MatchStrategy | None = None

    @property
    def matched(self) -> bool:
        return self.sidecar is not None


def _clip(name: str) -> str:
    """Apply Google's 46-character clip to a sidecar name, excluding ``.json``."""
    return name[:SIDECAR_FILENAME_BUDGET]


def _split_duplicate(filename: str) -> tuple[str, str] | None:
    """Split ``IMG_1234(1).jpg`` into ``("IMG_1234.jpg", "(1)")``.

    Google moves the duplicate marker to the *end* of the sidecar name, after the clipped
    portion - so the marker has to be lifted off the media name before deriving anything.
    """
    match = _DUPLICATE_RE.match(filename)
    if match is None:
        return None
    stem = match.group("stem")
    suffix = match.group("suffix") or ""
    if not stem:
        return None
    return f"{stem}{suffix}", match.group("marker")


def _strip_edited_marker(filename: str) -> str | None:
    """Return the original filename for an edited copy, or ``None`` if not one."""
    path = PurePath(filename)
    stem, suffix = path.stem, path.suffix
    for marker in EDITED_SUFFIXES:
        if stem.lower().endswith(marker.lower()):
            return f"{stem[: -len(marker)]}{suffix}"
    return None


def _candidates(media_filename: str) -> list[tuple[str, MatchStrategy]]:
    """Every sidecar name Google might have produced, each with the rule that produced it.

    Ordered most-specific first, so the first hit is the best one. Names are
    de-duplicated while preserving order: for a short filename the clipped form is
    identical to the full one, and the first (more specific) label should win.
    """
    found: list[tuple[str, MatchStrategy]] = []
    seen: set[str] = set()

    def add(name: str, strategy: MatchStrategy) -> None:
        if name not in seen:
            seen.add(name)
            found.append((name, strategy))

    stem = PurePath(media_filename).stem

    add(f"{media_filename}{SUPPLEMENTAL}.json", MatchStrategy.SUPPLEMENTAL)
    add(f"{_clip(f'{media_filename}{SUPPLEMENTAL}')}.json", MatchStrategy.TRUNCATED)
    add(f"{media_filename}.json", MatchStrategy.LEGACY)
    add(f"{_clip(media_filename)}.json", MatchStrategy.TRUNCATED)
    add(f"{stem}.json", MatchStrategy.STEM)

    # Duplicate markers: `IMG_1234(1).jpg` -> `IMG_1234.jpg.supplemental-metadata(1).json`.
    # The marker is never clipped; it is re-attached after the clipped portion.
    duplicate = _split_duplicate(media_filename)
    if duplicate is not None:
        clean, marker = duplicate
        add(f"{_clip(f'{clean}{SUPPLEMENTAL}')}{marker}.json", MatchStrategy.DUPLICATE)
        add(f"{clean}{SUPPLEMENTAL}{marker}.json", MatchStrategy.DUPLICATE)
        add(f"{_clip(clean)}{marker}.json", MatchStrategy.DUPLICATE)
        add(f"{clean}{marker}.json", MatchStrategy.DUPLICATE)

    return found


def candidate_sidecar_names(media_filename: str) -> list[str]:
    """Every sidecar name Google might have produced for ``media_filename``.

    Ordered most-specific first. See :func:`_candidates` for the rule behind each.
    """
    return [name for name, _ in _candidates(media_filename)]


def _lookup(filename: str, available: dict[str, Path]) -> tuple[Path, MatchStrategy] | None:
    """First sidecar in ``available`` matching any candidate name for ``filename``.

    Lookups are case-insensitive: Takeout is inconsistent about extension case, and the
    archive may have been unpacked on a case-insensitive filesystem.
    """
    for name, strategy in _candidates(filename):
        found = available.get(name.lower())
        if found is not None:
            return found, strategy
    return None


def match_media(media: Path, available: dict[str, Path]) -> SidecarMatch:
    """Pair one media file against ``available``, a lowercased-name index of sidecars."""
    direct = _lookup(media.name, available)
    if direct is not None:
        return SidecarMatch(media, direct[0], direct[1])

    # An edited copy has no sidecar of its own; it shares the original's.
    original = _strip_edited_marker(media.name)
    if original is not None:
        edited = _lookup(original, available)
        if edited is not None:
            return SidecarMatch(media, edited[0], MatchStrategy.EDITED)

    # A live photo's video half has no sidecar either - Takeout writes one JSON for the
    # still image only, so the video inherits it.
    if media.suffix.lower() in MOTION_SUFFIXES:
        for still in STILL_SUFFIXES:
            live = _lookup(f"{media.stem}{still}", available)
            if live is not None:
                return SidecarMatch(media, live[0], MatchStrategy.LIVE_PHOTO)

    return SidecarMatch(media, None, None)


def match_directory(
    media_files: Iterable[Path],
    sidecar_files: Iterable[Path],
) -> list[SidecarMatch]:
    """Pair every media file with a sidecar, reporting the ones that could not be paired.

    Unmatched media is never dropped: it comes back with ``sidecar=None`` so the caller
    records it and the final report can show it.
    """
    available = {path.name.lower(): path for path in sidecar_files}
    return [match_media(media, available) for media in media_files]
