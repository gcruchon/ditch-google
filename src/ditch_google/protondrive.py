"""Wrapper around the official ``proton-drive`` CLI.

Everything that talks to Proton goes through here, so the rest of the codebase never has
to know the CLI's argument shapes. Those shapes are new - photo and album support landed
in cli/v0.7.0 on 2026-07-30 - and this is the module that will need updating when they
change. See docs/adr/0002-why-proton-cli-over-rclone-protondrive.md.

Facts about the CLI that shape this module:

* Paths are virtual and always POSIX: ``/photos/...`` is the timeline, ``/albums/...``
  the albums, ``/my-files/...`` ordinary Drive files.
* ``photo upload`` **prompts interactively** unless ``--conflict-strategy`` is given.
  An unattended migration must always pass it, or it will hang forever waiting on stdin.
* ``photo upload`` reports a *summary*, not per-file identities. Failures are named, so a
  batch can still be attributed; successes cannot be mapped back to node UIDs.
* Uploads are de-duplicated server-side on name plus SHA1. With ``skip`` that makes a
  re-run naturally idempotent - already-uploaded photos are skipped rather than doubled.
* A batch with any failure exits non-zero, so the summary has to be read even when the
  command "fails", or a partial success would be reported as a total loss.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ditch_google import proc

__all__ = [
    "CONFLICT_STRATEGIES",
    "UploadSummary",
    "add_photos_to_album",
    "create_album",
    "is_authenticated",
    "list_albums",
    "parse_upload_summary",
    "timeline",
    "upload_photos",
]

#: What ``--conflict-strategy`` accepts. ``skip`` is our default: combined with
#: server-side name+SHA1 de-duplication it makes a resumed run idempotent.
CONFLICT_STRATEGIES: tuple[str, ...] = ("skip", "keep-both")

#: The virtual root of the Photos timeline.
PHOTOS_ROOT = "/photos"

#: The virtual root of albums.
ALBUMS_ROOT = "/albums"


@dataclass(frozen=True)
class UploadSummary:
    """Outcome of one ``photo upload`` invocation."""

    transferred: int = 0
    transferred_bytes: int = 0
    skipped: int = 0
    failed: int = 0
    #: Basename -> error message, for the files the CLI named as failures.
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> int:
        """Files now present in Proton: freshly uploaded plus already-there duplicates."""
        return self.transferred + self.skipped


def parse_upload_summary(stdout: str) -> UploadSummary:
    """Read the JSON summary ``photo upload --json`` prints on its last line.

    The CLI may log other lines first, so the summary is found by scanning backwards for
    the last parseable JSON object rather than assuming it is alone on stdout.
    """
    for line in reversed([line for line in stdout.splitlines() if line.strip()]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "transferredItems" not in payload:
            continue

        failures: dict[str, str] = {}
        for entry in payload.get("failures") or []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                failures[entry["name"]] = str(entry.get("error", "upload failed"))

        return UploadSummary(
            transferred=int(payload.get("transferredItems", 0)),
            transferred_bytes=int(payload.get("transferredBytes", 0)),
            skipped=int(payload.get("skippedItems", 0)),
            failed=int(payload.get("failedItems", 0)),
            failures=failures,
        )
    return UploadSummary()


def upload_photos(
    paths: Sequence[Path],
    *,
    conflict: str = "skip",
    on_line: Callable[[str], None] | None = None,
    timeout: float | None = None,
) -> UploadSummary:
    """Upload photos into the Proton Photos timeline.

    Args:
        paths: Local files to upload. Folder structure is flattened by the CLI, which is
            harmless - the timeline is date-driven and albums are restored separately.
        conflict: ``skip`` or ``keep-both``. Never omitted: without it the CLI prompts on
            stdin and an unattended run would hang.
        on_line: Called with each stdout line, for progress.
        timeout: Seconds, or ``None`` for no limit. Uploads legitimately take hours.

    Raises:
        ValueError: ``conflict`` is not a strategy the CLI accepts.
        proc.ToolNotFoundError: ``proton-drive`` is not installed.
    """
    if conflict not in CONFLICT_STRATEGIES:
        raise ValueError(f"conflict must be one of {CONFLICT_STRATEGIES}, got {conflict!r}")
    if not paths:
        return UploadSummary()

    result = proc.stream(
        [
            "proton-drive",
            "photo",
            "upload",
            "--conflict-strategy",
            conflict,
            "--json",
            *(str(path) for path in paths),
        ],
        on_line=on_line,
        timeout=timeout,
        # A batch containing any failure exits non-zero. Raising here would discard the
        # summary and report a partial success as a total loss.
        check=False,
    )

    summary = parse_upload_summary(result.stdout)
    if not result.ok and summary == UploadSummary():
        # Non-zero *and* no parseable summary means the command failed outright.
        raise proc.ToolFailedError(result.argv, result.returncode, result.stderr)
    return summary


def _run_json(args: Sequence[str], *, timeout: float = 120.0) -> Any:
    return proc.run_json(["proton-drive", *args, "--json"], timeout=timeout)


def is_authenticated() -> bool:
    """Whether a Proton session exists. Uses the cheapest command that proves it."""
    try:
        proc.run(["proton-drive", "album", "list", "--json"], timeout=60)
    except proc.ToolError:
        return False
    return True


def list_albums() -> list[dict[str, Any]]:
    """Every album in the account."""
    payload = _run_json(["album", "list"])
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def create_album(name: str) -> None:
    """Create an album. The caller is responsible for not creating one twice."""
    proc.run(["proton-drive", "album", "create", name, "--json"], timeout=120)


def add_photos_to_album(album_path: str, photo_paths: Sequence[str]) -> None:
    """Add photos to an album, both addressed by virtual path."""
    if not photo_paths:
        return
    proc.run(
        ["proton-drive", "album", "add-photo", album_path, *photo_paths, "--json"],
        timeout=600,
    )


def timeline() -> list[dict[str, Any]]:
    """Every photo in the timeline, for reconciliation."""
    payload = _run_json(["photo", "timeline"], timeout=900)
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def album_path(name: str) -> str:
    """The virtual path of an album."""
    return f"{ALBUMS_ROOT}/{name}"


def photo_path(name: str) -> str:
    """The virtual path of a photo in the timeline."""
    return f"{PHOTOS_ROOT}/{name}"
