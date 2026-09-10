"""The whole migration, one archive at a time.

A Takeout export of a real library is hundreds of gigabytes. Running the stages one after
another over the whole export would need that much local disk **twice** - the archives
plus everything unpacked out of them - which is more than most people have spare.

So the pipeline streams instead: each archive is fetched, unpacked, catalogued, repaired,
uploaded, and then **deleted** before the next one starts. Peak disk stays at roughly
twice the largest single part rather than twice the library.

Two things have to happen outside that loop, because they need the whole export:

* **Albums.** Takeout scatters one album across several parts, so an album is only
  complete at the end. Membership is *recorded* per archive while the files are still on
  disk; the push to Proton reads only the ledger, so it still works afterwards.
* **Verification.** Listing the Proton timeline is one expensive call, so it happens once
  rather than per archive.

Everything is resumable. The ledger knows which stage each archive and item reached, so
re-running skips what is done. Deleting local files after upload is the one thing that
cannot be undone cheaply - use ``keep_local`` to retain them.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ditch_google.photos import albums as albums_module
from ditch_google.photos import discover, fetch, fix, unpack, upload, verify
from ditch_google.photos.report import Report, build_report
from ditch_google.proc import ToolError
from ditch_google.state import ArchiveStage, State

__all__ = ["PipelineResult", "run_migration"]


@dataclass
class PipelineResult:
    """What a full run did."""

    archives_processed: int = 0
    archives_failed: int = 0
    media_found: int = 0
    metadata_written: int = 0
    uploaded: int = 0
    already_present: int = 0
    albums_created: int = 0
    photos_added_to_albums: int = 0
    bytes_reclaimed: int = 0
    report: Report | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)


#: Called with (stage, archive name, detail) so a front end can show progress without
#: this module knowing anything about how it is displayed.
ProgressHook = Callable[[str, str, str], None]


def _emit(hook: ProgressHook | None, stage: str, archive: str, detail: str = "") -> None:
    if hook is not None:
        hook(stage, archive, detail)


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _reclaim(work_dir: Path, archive_name: str) -> int:
    """Delete an archive and its unpacked tree, returning the bytes freed.

    This is what keeps peak disk bounded. It runs only after the archive's photos are in
    Proton, so the copy being deleted is never the only one.
    """
    freed = 0
    archive_file = work_dir / archive_name
    unpacked = work_dir / "unpacked" / archive_name

    if archive_file.is_file():
        freed += archive_file.stat().st_size
        archive_file.unlink()
    if unpacked.is_dir():
        freed += _directory_size(unpacked)
        shutil.rmtree(unpacked, ignore_errors=True)
    return freed


def _process_archive(
    state: State,
    archive_name: str,
    work_dir: Path,
    *,
    conflict: str,
    transfers: int,
    prefer_existing: bool,
    keep_local: bool,
    result: PipelineResult,
    hook: ProgressHook | None,
) -> None:
    """Take one archive all the way from Drive to Proton."""
    record = state.get_archive(archive_name)
    if record is None:
        return

    if record.stage is ArchiveStage.PENDING:
        _emit(hook, "fetch", archive_name)
        fetch.fetch_archive(state, archive_name, work_dir, transfers=transfers)

    record = state.get_archive(archive_name)
    if record is not None and record.stage is ArchiveStage.FETCHED:
        _emit(hook, "unpack", archive_name)
        unpacked = unpack.unpack_for_state(state, archive_name, work_dir)
        found = discover.discover_archive(state, archive_name, unpacked.destination)
        result.media_found += found.media
        _emit(hook, "discover", archive_name, f"{found.media} media, {found.matched} matched")

    unpacked_root = work_dir / "unpacked" / archive_name

    # Read album membership while the files are still here; the push happens at the end.
    albums_module.record_albums(state, [unpacked_root])

    _emit(hook, "fix", archive_name)
    fixed = fix.fix_pending(state, archive=archive_name, prefer_existing=prefer_existing)
    result.metadata_written += fixed.written

    _emit(hook, "upload", archive_name)
    sent = upload.upload_pending(state, archive=archive_name, conflict=conflict)
    result.uploaded += sent.uploaded
    result.already_present += sent.already_present

    if not keep_local:
        freed = _reclaim(work_dir, archive_name)
        result.bytes_reclaimed += freed
        _emit(hook, "reclaim", archive_name, f"{freed} bytes freed")

    # Only now is the archive done. Marking it earlier would let a resumed run skip work
    # that never actually happened.
    state.set_archive_stage(archive_name, ArchiveStage.COMPLETED)
    result.archives_processed += 1


def run_migration(
    state: State,
    *,
    source: str,
    work_dir: Path,
    conflict: str = "skip",
    transfers: int = 4,
    prefer_existing: bool = False,
    keep_local: bool = False,
    marker: bool = True,
    check_remote: bool = True,
    hook: ProgressHook | None = None,
) -> PipelineResult:
    """Run the whole migration, streaming one archive at a time.

    Safe to re-run: every stage is idempotent and the ledger records what is done.
    """
    result = PipelineResult()

    _emit(hook, "list", source)
    archives = fetch.list_archives(source)
    fetch.register_archives(state, archives)

    while (pending := state.next_archive()) is not None:
        try:
            _process_archive(
                state,
                pending.name,
                work_dir,
                conflict=conflict,
                transfers=transfers,
                prefer_existing=prefer_existing,
                keep_local=keep_local,
                result=result,
                hook=hook,
            )
        except (ToolError, OSError, ValueError) as exc:
            # One bad archive must not abandon the rest of the library. It is marked
            # failed with its reason, which also stops next_archive returning it again.
            state.set_archive_stage(pending.name, ArchiveStage.FAILED, error=str(exc))
            result.archives_failed += 1
            result.errors.append((pending.name, str(exc)))
            _emit(hook, "failed", pending.name, str(exc))

    _emit(hook, "albums", "")
    album_result = albums_module.push_albums(state, marker=marker)
    result.albums_created += album_result.created
    result.photos_added_to_albums += album_result.photos_added
    result.errors.extend(album_result.failed)

    if check_remote:
        _emit(hook, "verify", "")
        verify.verify_uploads(state)
    else:
        verify.close_finished_archives(state)

    result.report = build_report(state)
    return result
