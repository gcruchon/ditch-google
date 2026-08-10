"""Reconcile what is in Proton against what the ledger says should be.

There is no checksum comparison available: ``photo timeline`` returns node UIDs and
capture times, not names or hashes, and the CLI never told us the UIDs it created. So
verification reconciles **counts and capture times** rather than content.

That is a weaker guarantee than the rest of the pipeline offers, and the report says so.
It is still worth doing: it catches the failure that actually matters - photos the ledger
believes were uploaded but which are not in the timeline at all.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ditch_google import protondrive
from ditch_google.proc import ToolError
from ditch_google.state import ArchiveStage, ItemStage, State

__all__ = ["VerifyResult", "close_finished_archives", "verify_uploads"]


@dataclass
class VerifyResult:
    """Outcome of reconciling the ledger against the Proton timeline."""

    expected: int = 0
    timeline_total: int = 0
    matched_capture_times: int = 0
    missing_capture_times: list[str] = field(default_factory=list)
    checked: bool = True

    @property
    def ok(self) -> bool:
        return self.checked and not self.missing_capture_times


def verify_uploads(state: State, *, sample: int | None = None) -> VerifyResult:
    """Check that photos the ledger calls uploaded are present in the timeline.

    Matching is by capture time, the only attribute both sides expose. That makes the
    check approximate: several photos can share a capture time, and a photo without one
    cannot be matched at all. It is treated as a signal, not a proof, and the report
    presents it that way.

    Args:
        sample: Only check this many items. The full timeline listing is cheap, but
            reading a sidecar per item is not on a large library.
    """
    uploaded = [
        item
        for item in state.items()
        if item.stage in {ItemStage.UPLOADED, ItemStage.VERIFIED} and item.taken_at
    ]
    if sample is not None:
        uploaded = uploaded[:sample]

    result = VerifyResult(expected=len(uploaded))
    if not uploaded:
        return result

    try:
        timeline = protondrive.timeline()
    except ToolError:
        # Not being able to reach Proton is "unverified", not "verification failed".
        # The report distinguishes the two so a network blip never reads as data loss.
        result.checked = False
        return result

    result.timeline_total = len(timeline)
    remote_times = Counter(
        str(entry.get("captureTime", ""))[:19] for entry in timeline if entry.get("captureTime")
    )

    for item in uploaded:
        assert item.taken_at is not None  # noqa: S101 - filtered above
        try:
            taken = datetime.fromisoformat(item.taken_at)
        except ValueError:
            continue
        key = taken.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if remote_times.get(key):
            remote_times[key] -= 1
            result.matched_capture_times += 1
            # `verified` is terminal: confirming a photo is in Proton is what finishes
            # it. Without this promotion an item sits at `uploaded` forever and a
            # successful migration can never report as complete.
            state.set_item_stage(item.source_path, ItemStage.VERIFIED)
        else:
            result.missing_capture_times.append(Path(item.source_path).name)

    close_finished_archives(state)
    return result


def close_finished_archives(state: State) -> int:
    """Mark archives completed once none of their items still need work.

    An archive is only done when its contents are, so this is derived rather than set by
    whichever stage happened to touch it last.
    """
    closed = 0
    for archive in state.archives():
        if archive.stage in {ArchiveStage.COMPLETED, ArchiveStage.FAILED}:
            continue
        if state.pending_items(archive.name):
            continue
        if not state.items(archive=archive.name):
            continue
        state.set_archive_stage(archive.name, ArchiveStage.COMPLETED)
        closed += 1
    return closed
