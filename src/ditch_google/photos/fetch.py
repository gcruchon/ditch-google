"""Download the Takeout export from Google Drive with rclone.

Google Takeout can deliver an export straight to Drive, which is the only part of this
migration Google lets us automate - see ADR-0001. rclone then pulls the archives down,
resuming any that were interrupted.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ditch_google import proc
from ditch_google.state import ArchiveStage, State

__all__ = [
    "ARCHIVE_SUFFIXES",
    "RemoteArchive",
    "TransferProgress",
    "fetch_archive",
    "list_archives",
    "register_archives",
]

#: Takeout delivers `.tgz` or `.zip`, depending on the option chosen at export time.
ARCHIVE_SUFFIXES: tuple[str, ...] = (".tgz", ".tar.gz", ".zip")


@dataclass(frozen=True)
class RemoteArchive:
    """One Takeout part sitting in Google Drive."""

    name: str
    size_bytes: int
    remote_path: str


@dataclass(frozen=True)
class TransferProgress:
    """A snapshot from rclone's JSON stats."""

    bytes_done: int
    bytes_total: int
    speed_bps: float
    eta_seconds: float | None

    @property
    def fraction(self) -> float:
        if self.bytes_total <= 0:
            return 0.0
        return min(1.0, self.bytes_done / self.bytes_total)


def _is_archive(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def list_archives(source: str) -> list[RemoteArchive]:
    """List the Takeout archives at ``source``, e.g. ``drive:Takeout``.

    Non-archive files are ignored rather than treated as an error: Takeout often leaves
    an HTML index or a README alongside the parts.
    """
    entries = proc.run_json(
        ["rclone", "lsjson", source, "--files-only", "--no-modtime", "--no-mimetype"],
        timeout=300,
    )
    archives = [
        RemoteArchive(
            name=str(entry["Name"]),
            size_bytes=int(entry.get("Size", -1)),
            remote_path=f"{source.rstrip('/')}/{entry['Path']}",
        )
        for entry in entries
        if _is_archive(str(entry["Name"]))
    ]
    return sorted(archives, key=lambda archive: archive.name)


def register_archives(state: State, archives: Sequence[RemoteArchive]) -> int:
    """Record archives in the ledger. Idempotent - safe to re-run on every resume."""
    with state.transaction():
        for archive in archives:
            state.add_archive(archive.name, archive.remote_path, archive.size_bytes)
    return len(archives)


def parse_progress(line: str) -> TransferProgress | None:
    """Pull a progress snapshot out of one rclone JSON log line.

    rclone writes these to stderr, interleaved with ordinary log messages, so anything
    without a `stats` object is skipped rather than treated as malformed.
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None

    eta = stats.get("eta")
    return TransferProgress(
        bytes_done=int(stats.get("bytes", 0)),
        bytes_total=int(stats.get("totalBytes", 0)),
        speed_bps=float(stats.get("speed", 0.0)),
        eta_seconds=float(eta) if eta is not None else None,
    )


def fetch_archive(
    state: State,
    archive_name: str,
    work_dir: Path,
    *,
    on_progress: Callable[[TransferProgress], None] | None = None,
    transfers: int = 4,
) -> Path:
    """Download one archive into ``work_dir`` and mark it fetched.

    rclone is itself resumable and skips a file already present at the right size, so
    re-running after an interruption costs a listing rather than a re-download.
    """
    record = state.get_archive(archive_name)
    if record is None:
        raise KeyError(f"{archive_name!r} is not in the ledger")

    destination = work_dir / archive_name
    work_dir.mkdir(parents=True, exist_ok=True)

    def _handle(line: str) -> None:
        if on_progress is None:
            return
        progress = parse_progress(line)
        if progress is not None:
            on_progress(progress)

    try:
        proc.stream(
            [
                "rclone",
                "copyto",
                record.remote_path,
                str(destination),
                "--use-json-log",
                "--stats",
                "1s",
                "--stats-log-level",
                "NOTICE",
                "--transfers",
                str(transfers),
            ],
            on_stderr_line=_handle,
        )
    except proc.ToolError as exc:
        state.set_archive_stage(archive_name, ArchiveStage.FAILED, error=str(exc))
        raise

    state.set_archive_stage(archive_name, ArchiveStage.FETCHED)
    return destination
