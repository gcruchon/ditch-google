"""Upload fixed media into the Proton Photos timeline."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ditch_google import protondrive
from ditch_google.state import Item, ItemStage, State

__all__ = ["DEFAULT_BATCH_SIZE", "UploadResult", "batch_items", "upload_pending"]

#: Files per ``photo upload`` invocation. Large enough that process startup is amortised,
#: small enough that an interrupted run loses little and progress stays informative.
DEFAULT_BATCH_SIZE = 50


@dataclass
class UploadResult:
    """Outcome of an upload pass."""

    uploaded: int = 0
    already_present: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.uploaded + self.already_present + self.failed


def batch_items(items: Sequence[Item], size: int = DEFAULT_BATCH_SIZE) -> Iterator[list[Item]]:
    """Group items into batches that contain no repeated basename.

    The CLI reports failures by basename only. Two files called ``IMG_1234.jpg`` from
    different Takeout parts in the same batch would make a failure impossible to
    attribute, so they are placed in different batches instead. Marking the wrong photo
    as failed - or worse, the wrong one as uploaded - is not a trade worth making for a
    slightly fuller batch.
    """
    current: list[Item] = []
    seen: set[str] = set()

    for item in items:
        name = Path(item.source_path).name
        if len(current) >= size or name in seen:
            yield current
            current, seen = [], set()
        current.append(item)
        seen.add(name)

    if current:
        yield current


def upload_pending(
    state: State,
    *,
    archive: str | None = None,
    conflict: str = "skip",
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_progress: Callable[[int, int], None] | None = None,
) -> UploadResult:
    """Upload every item that has been fixed but not yet uploaded.

    Resume is cheap and safe: Proton de-duplicates on name plus SHA1, so a re-run with
    the default ``skip`` strategy leaves already-uploaded photos alone rather than
    creating a second copy of each.
    """
    pending = [item for item in state.pending_items(archive) if item.stage is ItemStage.FIXED]
    result = UploadResult()
    if not pending:
        return result

    done = 0
    for batch in batch_items(pending, batch_size):
        _upload_batch(state, batch, conflict=conflict, result=result)
        done += len(batch)
        if on_progress is not None:
            on_progress(done, len(pending))

    return result


def _upload_batch(
    state: State,
    batch: Sequence[Item],
    *,
    conflict: str,
    result: UploadResult,
) -> None:
    summary = protondrive.upload_photos(
        [Path(item.source_path) for item in batch], conflict=conflict
    )

    # The CLI silently ignores anything whose media type is not image/* or video/* - a
    # Takeout archive can contain such files. When the counts do not add up, some file in
    # this batch was neither uploaded, skipped nor reported as failed, and we cannot tell
    # which. Marking the whole batch uploaded would claim a photo reached Proton when it
    # did not, so the batch is retried one file at a time to attribute it precisely.
    accounted = summary.transferred + summary.skipped + summary.failed
    if len(batch) > 1 and accounted != len(batch):
        for item in batch:
            _upload_batch(state, [item], conflict=conflict, result=result)
        return

    for item in batch:
        name = Path(item.source_path).name
        error = summary.failures.get(name)
        if error is not None:
            state.set_item_stage(item.source_path, ItemStage.FAILED, error=error)
            result.failed += 1
        elif accounted != len(batch):
            # Single-file batch that the CLI ignored entirely: it is not in Proton and it
            # never will be without intervention, so say so rather than claiming success.
            state.set_item_stage(
                item.source_path,
                ItemStage.FAILED,
                error="proton-drive ignored this file (unsupported media type)",
            )
            result.failed += 1
        else:
            state.set_item_stage(item.source_path, ItemStage.UPLOADED)

    # Counts come from the CLI's own summary; stages come from name matching. A
    # server-side duplicate is reported as "skipped" and still means the photo is in
    # Proton, so it counts as already present rather than as work done.
    result.uploaded += summary.transferred
    result.already_present += summary.skipped
