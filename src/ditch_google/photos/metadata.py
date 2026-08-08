"""Write Takeout sidecar metadata back into the media files.

This is the stage that makes the migration worth running. Proton's Drive SDK derives a
photo's ``captureTime``, location and camera details **from the file's own EXIF at upload
time**. Takeout strips exactly those fields out into sidecar JSON. So without this stage
the entire library lands in the Proton timeline dated the day it was uploaded, with no
locations - technically transferred, practically ruined.

Two decisions worth knowing about:

**The sidecar wins by default.** ``photoTakenTime`` is the timestamp Google Photos itself
displays, so trusting it is what makes the Proton timeline match the library the user is
looking at today. Google's own upload pipeline sometimes overwrote a file's original EXIF
date with the *upload* date, so preferring the embedded value would faithfully reproduce
Google's mistakes. Pass ``prefer_existing=True`` to fill only the gaps instead.

**Times are written as local wall-clock time, not UTC.** This is not the obvious choice
and was corrected after testing against a real Proton account. Takeout gives a Unix
timestamp - an instant - and the original capture timezone is not in the export. Writing
that instant as UTC with an explicit ``OffsetTimeOriginal=+00:00`` looked like the honest
option, but Proton's SDK **ignores the offset tag** and interprets ``DateTimeOriginal`` as
local time. A photo written as ``13:20`` UTC on a machine in CEST arrived in the Proton
timeline at ``11:20Z`` - shifted by the migrating machine's UTC offset, which for a user
in UTC+13 would move photos to the wrong day.

So the timestamp is converted to a wall-clock time in a chosen timezone (the machine's by
default, overridable) and the matching offset is written alongside. Proton then recovers
the correct instant, and readers that do honour offsets still see an unambiguous value.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from ditch_google.exiftool import ExiftoolError, ExiftoolSession

__all__ = [
    "SidecarData",
    "build_exiftool_args",
    "load_sidecar",
    "parse_sidecar",
    "write_metadata",
]

#: Extensions written with QuickTime tags as well as EXIF.
VIDEO_SUFFIXES: frozenset[str] = frozenset(
    {".mp4", ".mov", ".m4v", ".3gp", ".mts", ".avi", ".mkv", ".webm", ".mp"}
)


@dataclass(frozen=True)
class SidecarData:
    """The fields worth recovering from a Takeout sidecar."""

    taken_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    description: str | None = None
    people: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def is_empty(self) -> bool:
        return not (self.taken_at or self.has_location or self.description or self.people)


def _coordinates(block: Any) -> tuple[float, float, float] | None:
    """Read a geo block, treating an all-zero one as absent.

    Google zeroes ``geoData`` rather than omitting it when a photo has no location - which
    is common, since it happens whenever location history was off. Writing 0,0 would
    place every such photo in the Gulf of Guinea.
    """
    if not isinstance(block, dict):
        return None
    try:
        latitude = float(block.get("latitude", 0.0))
        longitude = float(block.get("longitude", 0.0))
        altitude = float(block.get("altitude", 0.0))
    except (TypeError, ValueError):
        return None
    # Exact comparison is deliberate, not an oversight. Google writes a literal 0.0 here
    # as its "no location" marker, so this is a sentinel check rather than a measurement
    # comparison. A tolerance would additionally discard the real Null Island coordinates,
    # and more importantly would start rejecting genuine locations near the equator.
    if latitude == 0.0 and longitude == 0.0:
        return None
    return latitude, longitude, altitude


def parse_sidecar(payload: Any) -> SidecarData:
    """Extract the useful fields from a parsed sidecar document.

    Unknown or malformed fields are skipped rather than raising: a sidecar with a broken
    ``geoData`` should still contribute its timestamp.
    """
    if not isinstance(payload, dict):
        return SidecarData()

    taken_at: datetime | None = None
    taken = payload.get("photoTakenTime")
    if isinstance(taken, dict):
        try:
            taken_at = datetime.fromtimestamp(int(taken["timestamp"]), tz=UTC)
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            taken_at = None

    # `geoData` is the user-visible location; `geoDataExif` is what the camera recorded.
    # Prefer the former, fall back to the latter when Google zeroed it.
    coordinates = _coordinates(payload.get("geoData")) or _coordinates(payload.get("geoDataExif"))

    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        description = None

    people: list[str] = []
    raw_people = payload.get("people")
    if isinstance(raw_people, list):
        people = [
            person["name"]
            for person in raw_people
            if isinstance(person, dict) and isinstance(person.get("name"), str)
        ]

    return SidecarData(
        taken_at=taken_at,
        latitude=coordinates[0] if coordinates else None,
        longitude=coordinates[1] if coordinates else None,
        altitude=coordinates[2] if coordinates else None,
        description=description,
        people=tuple(people),
    )


def load_sidecar(path: Path) -> SidecarData:
    """Read and parse a sidecar file. A malformed file yields empty data, not an error."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return SidecarData()
    return parse_sidecar(payload)


def _time_args(data: SidecarData, *, is_video: bool, timezone: tzinfo | None = None) -> list[str]:
    if data.taken_at is None:
        return []

    # Convert the instant to wall-clock time in the target zone. Proton reads
    # DateTimeOriginal as local time and ignores the offset tag, so writing UTC here
    # shifts every photo by the machine's offset. See this module's docstring.
    local = data.taken_at.astimezone(timezone)
    stamp = local.strftime("%Y:%m:%d %H:%M:%S")
    offset = local.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"

    args = [
        f"-EXIF:DateTimeOriginal={stamp}",
        f"-EXIF:CreateDate={stamp}",
        f"-EXIF:ModifyDate={stamp}",
        f"-XMP:DateCreated={stamp}",
        # Recorded so the file is self-describing for readers that do honour it, even
        # though Proton does not.
        f"-EXIF:OffsetTimeOriginal={offset}",
        f"-EXIF:OffsetTimeDigitized={offset}",
    ]
    if is_video:
        # EXIF tags are not read from most containers; QuickTime atoms are.
        args += [
            f"-QuickTime:CreateDate={stamp}",
            f"-QuickTime:ModifyDate={stamp}",
            f"-QuickTime:TrackCreateDate={stamp}",
            f"-QuickTime:MediaCreateDate={stamp}",
        ]
    return args


def _location_args(data: SidecarData) -> list[str]:
    if not data.has_location:
        return []

    assert data.latitude is not None  # noqa: S101 - guarded by has_location
    assert data.longitude is not None  # noqa: S101

    # EXIF stores magnitudes plus a hemisphere reference. Writing a signed value without
    # the matching ref would put Sydney in the North Atlantic.
    args = [
        f"-EXIF:GPSLatitude={abs(data.latitude)}",
        f"-EXIF:GPSLatitudeRef={'N' if data.latitude >= 0 else 'S'}",
        f"-EXIF:GPSLongitude={abs(data.longitude)}",
        f"-EXIF:GPSLongitudeRef={'E' if data.longitude >= 0 else 'W'}",
    ]
    if data.altitude:
        args += [
            f"-EXIF:GPSAltitude={abs(data.altitude)}",
            f"-EXIF:GPSAltitudeRef={'0' if data.altitude >= 0 else '1'}",
        ]
    return args


def _text_args(data: SidecarData) -> list[str]:
    args: list[str] = []
    if data.description:
        args += [
            f"-EXIF:ImageDescription={data.description}",
            f"-XMP:Description={data.description}",
        ]
    args += [f"-XMP:PersonInImage+={person}" for person in data.people]
    return args


def build_exiftool_args(
    data: SidecarData,
    media: Path,
    *,
    prefer_existing: bool = False,
    timezone: tzinfo | None = None,
) -> list[str]:
    """Build the exiftool arguments that write ``data`` into ``media``.

    Returns an empty list when there is nothing to write, so the caller can skip the file
    entirely rather than paying for a no-op command.
    """
    if data.is_empty:
        return []

    args = [
        *_time_args(data, is_video=media.suffix.lower() in VIDEO_SUFFIXES, timezone=timezone),
        *_location_args(data),
        *_text_args(data),
    ]

    if not args:
        return []

    # `-wm cg` creates missing tags but never updates existing ones.
    if prefer_existing:
        args += ["-wm", "cg"]

    args += [
        # Keep the file in place; a temp-file rewrite would double peak disk use during a
        # stage that already runs against tens of gigabytes.
        "-overwrite_original_in_place",
        # Takeout files routinely carry minor structural warnings. Refusing to write them
        # would abandon a large share of a real library.
        "-m",
        str(media),
    ]
    return args


def write_metadata(
    session: ExiftoolSession,
    media: Path,
    data: SidecarData,
    *,
    prefer_existing: bool = False,
    timezone: tzinfo | None = None,
) -> bool:
    """Write ``data`` into ``media``. Returns whether anything was written.

    Also sets the file's modification time to the capture time, which gives tools that
    ignore EXIF - and any later manual inspection - a sensible ordering.

    Raises:
        ExiftoolError: exiftool refused the write.
    """
    args = build_exiftool_args(data, media, prefer_existing=prefer_existing, timezone=timezone)
    if not args:
        return False

    session.execute(args)

    if data.taken_at is not None:
        timestamp = data.taken_at.timestamp()
        # A filesystem that refuses utime shouldn't fail the item: the EXIF write, which
        # is what Proton actually reads, has already succeeded.
        with contextlib.suppress(OSError):
            os.utime(media, (timestamp, timestamp))

    return True


def describe_failure(error: ExiftoolError) -> str:
    """A short, storable reason for the ledger."""
    return str(error).strip() or "exiftool reported an unknown error"
