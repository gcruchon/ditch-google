"""Drive the metadata repair across every discovered item."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ditch_google.exiftool import ExiftoolError, ExiftoolSession
from ditch_google.photos import metadata as metadata_module
from ditch_google.state import ItemStage, State

__all__ = ["FixResult", "fix_pending"]


@dataclass
class FixResult:
    """Outcome of a metadata-repair pass."""

    written: int = 0
    skipped_no_sidecar: int = 0
    skipped_empty: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.written + self.skipped_no_sidecar + self.skipped_empty + self.failed


def fix_pending(
    state: State,
    *,
    prefer_existing: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> FixResult:
    """Write sidecar metadata into every item that has not been fixed yet.

    One exiftool session serves the whole pass - see :mod:`ditch_google.exiftool` for why
    that matters at library scale.

    An item that fails is recorded as failed with its reason and the pass continues. One
    unwritable photo out of a hundred thousand should not abandon the migration; the
    failures surface in the final report instead.
    """
    pending = [item for item in state.pending_items() if item.stage is ItemStage.DISCOVERED]
    result = FixResult()
    if not pending:
        return result

    # Stamped into every file so this migration stays identifiable afterwards, even
    # without the ledger and outside Proton.
    migration_id = state.migration_id

    with ExiftoolSession() as session:
        for index, item in enumerate(pending, start=1):
            media = Path(item.source_path)

            if item.sidecar_path is None:
                # Nothing to write, but the file still needs uploading, so it advances
                # rather than being treated as an error.
                state.set_item_stage(item.source_path, ItemStage.FIXED)
                result.skipped_no_sidecar += 1
            else:
                data = metadata_module.load_sidecar(Path(item.sidecar_path))
                try:
                    wrote = metadata_module.write_metadata(
                        session,
                        media,
                        data,
                        prefer_existing=prefer_existing,
                        migration_id=migration_id,
                    )
                except ExiftoolError as exc:
                    state.set_item_stage(
                        item.source_path,
                        ItemStage.FAILED,
                        error=metadata_module.describe_failure(exc),
                    )
                    result.failed += 1
                else:
                    state.set_item_stage(item.source_path, ItemStage.FIXED)
                    if wrote:
                        result.written += 1
                    else:
                        result.skipped_empty += 1

            if on_progress is not None:
                on_progress(index, len(pending))

    return result
