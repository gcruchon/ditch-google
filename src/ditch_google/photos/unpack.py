"""Extract Takeout archives safely.

An archive is **untrusted input**. Even though this one nominally came from Google, it
travelled through a download and sits on disk where anything could have altered it, so
extraction validates every member rather than trusting the archive's own paths.

Three classic attacks are refused explicitly:

* **Path traversal** - a member named ``../../.ssh/authorized_keys`` escaping the
  destination directory (Zip Slip / CVE-2007-4559).
* **Absolute paths** - a member named ``/etc/cron.d/evil``.
* **Links** - a symlink or hard link pointing outside the destination, which turns a
  later innocuous write into an arbitrary-file write.

Device nodes, FIFOs and other special members are skipped too: a photo archive has no
legitimate reason to contain them.
"""

from __future__ import annotations

import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ditch_google.state import ArchiveStage, State

__all__ = [
    "UnpackResult",
    "UnsafeArchiveMemberError",
    "is_safe_member",
    "unpack_archive",
]


class UnsafeArchiveMemberError(Exception):
    """An archive member tried to write outside the destination directory."""

    def __init__(self, member: str, reason: str) -> None:
        self.member = member
        self.reason = reason
        super().__init__(f"refusing to extract {member!r}: {reason}")


@dataclass
class UnpackResult:
    """What came out of an archive."""

    destination: Path
    extracted: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def skip(self, member: str, reason: str) -> None:
        self.skipped.append((member, reason))


def is_safe_member(name: str) -> tuple[bool, str]:
    """Check a member name is a plain relative path staying inside the destination.

    Returns ``(ok, reason)``; ``reason`` is empty when ``ok``.

    The check is done on the *name*, before any filesystem call, so a malicious entry is
    refused without ever touching disk.
    """
    if not name or name in {".", ".."}:
        return False, "empty or dot path"

    # Windows-style separators and drive letters, in case an archive was built there.
    normalised = name.replace("\\", "/")
    if len(normalised) > 1 and normalised[1] == ":":
        return False, "drive-letter absolute path"

    path = PurePosixPath(normalised)
    if path.is_absolute():
        return False, "absolute path"
    if any(part == ".." for part in path.parts):
        return False, "path traversal"
    return True, ""


def _resolves_inside(destination: Path, target: Path) -> bool:
    """Belt-and-braces check that ``target`` really lands under ``destination``.

    The name check above should already have caught this. This catches the case where a
    *previously extracted* symlink has made an otherwise-innocent relative path resolve
    somewhere else.
    """
    try:
        resolved_root = destination.resolve()
        resolved_target = (destination / target).resolve()
    except OSError:
        return False
    return resolved_target == resolved_root or resolved_root in resolved_target.parents


def _tar_members(archive: tarfile.TarFile, result: UnpackResult) -> Iterator[tarfile.TarInfo]:
    for member in archive.getmembers():
        ok, reason = is_safe_member(member.name)
        if not ok:
            raise UnsafeArchiveMemberError(member.name, reason)
        if member.issym() or member.islnk():
            result.skip(member.name, "link")
            continue
        if not (member.isfile() or member.isdir()):
            result.skip(member.name, "special file")
            continue
        if not _resolves_inside(result.destination, Path(member.name)):
            raise UnsafeArchiveMemberError(member.name, "resolves outside the destination")
        yield member


def _extract_tar(source: Path, result: UnpackResult) -> None:
    with tarfile.open(source, "r:*") as archive:
        for member in _tar_members(archive, result):
            # `filter="data"` is Python's own hardening: it strips ownership, refuses
            # absolute paths and links, and clamps permissions. We validate as well
            # rather than instead - it is available from 3.11.4 but not 3.11.0.
            try:
                archive.extract(member, result.destination, filter="data")
            except TypeError:  # pragma: no cover - only on 3.11.0 to 3.11.3
                archive.extract(member, result.destination)
            if member.isfile():
                result.extracted += 1


def _extract_zip(source: Path, result: UnpackResult) -> None:
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            ok, reason = is_safe_member(info.filename)
            if not ok:
                raise UnsafeArchiveMemberError(info.filename, reason)

            # The high byte of external_attr holds the Unix mode; 0xA000 is S_IFLNK.
            mode = info.external_attr >> 16
            if mode & 0xF000 == 0xA000:
                result.skip(info.filename, "link")
                continue
            if info.is_dir():
                continue
            if not _resolves_inside(result.destination, Path(info.filename)):
                raise UnsafeArchiveMemberError(info.filename, "resolves outside the destination")

            archive.extract(info, result.destination)
            result.extracted += 1


def unpack_archive(source: Path, destination: Path) -> UnpackResult:
    """Extract ``source`` into ``destination``, validating every member.

    Raises:
        UnsafeArchiveMemberError: A member tried to escape the destination. Nothing
            further is extracted - an archive containing one hostile entry is not one
            to keep unpacking.
        ValueError: The file is not a recognised archive format.
    """
    destination.mkdir(parents=True, exist_ok=True)
    result = UnpackResult(destination=destination)

    if tarfile.is_tarfile(source):
        _extract_tar(source, result)
    elif zipfile.is_zipfile(source):
        _extract_zip(source, result)
    else:
        raise ValueError(f"{source} is not a .tgz or .zip archive")

    return result


def unpack_for_state(state: State, archive_name: str, work_dir: Path) -> UnpackResult:
    """Unpack one archive from the ledger and record the outcome.

    Each archive extracts into its own directory. Takeout scatters a single album across
    several parts, so the tree is reassembled later rather than by overlaying extractions.
    """
    source = work_dir / archive_name
    destination = work_dir / "unpacked" / archive_name

    try:
        result = unpack_archive(source, destination)
    except (UnsafeArchiveMemberError, ValueError, OSError) as exc:
        state.set_archive_stage(archive_name, ArchiveStage.FAILED, error=str(exc))
        raise

    state.set_archive_stage(archive_name, ArchiveStage.UNPACKED)
    return result
