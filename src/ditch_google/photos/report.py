"""The final report.

The report is this tool's main trust signal. A migration that says "done" without saying
what it could *not* do is worse than useless - it invites someone to delete their Google
library on the strength of a green tick. So every shortfall is counted and named.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ditch_google.state import Item, ItemStage, State

__all__ = ["Report", "build_report"]


@dataclass
class Report:
    """What the migration achieved, and what it did not."""

    migration_id: str
    started_at: str
    archives_total: int = 0
    archives_completed: int = 0
    archives_failed: int = 0
    items_total: int = 0
    items_uploaded: int = 0
    items_failed: int = 0
    items_pending: int = 0
    without_sidecar: int = 0
    albums_total: int = 0
    albums_incomplete: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True only when nothing was left behind."""
        return (
            self.items_failed == 0
            and self.items_pending == 0
            and self.archives_failed == 0
            and self.albums_incomplete == 0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "migration_id": self.migration_id,
            "started_at": self.started_at,
            "clean": self.clean,
            "archives": {
                "total": self.archives_total,
                "completed": self.archives_completed,
                "failed": self.archives_failed,
            },
            "items": {
                "total": self.items_total,
                "uploaded": self.items_uploaded,
                "failed": self.items_failed,
                "pending": self.items_pending,
                "without_sidecar": self.without_sidecar,
            },
            "albums": {"total": self.albums_total, "incomplete": self.albums_incomplete},
            "failures": [{"item": name, "error": error} for name, error in self.failures],
        }


#: Cap on the failure list so a catastrophic run does not print a hundred thousand lines.
MAX_LISTED_FAILURES = 50


def build_report(state: State) -> Report:
    """Summarise the ledger."""
    archives = state.archives()
    items = state.items()
    albums = state.albums()

    failed: list[Item] = [i for i in items if i.stage is ItemStage.FAILED]
    uploaded = [i for i in items if i.stage in {ItemStage.UPLOADED, ItemStage.VERIFIED}]
    pending = state.pending_items()

    incomplete = sum(1 for album in albums if state.album_members(album.id, pending_only=True))

    return Report(
        migration_id=state.migration_id,
        started_at=state.started_at,
        archives_total=len(archives),
        archives_completed=sum(1 for a in archives if a.stage.value == "completed"),
        archives_failed=sum(1 for a in archives if a.stage.value == "failed"),
        items_total=len(items),
        items_uploaded=len(uploaded),
        items_failed=len(failed),
        items_pending=len(pending),
        without_sidecar=len(state.items_without_sidecar()),
        albums_total=len(albums),
        albums_incomplete=incomplete,
        failures=[
            (Path(item.source_path).name, item.error or "unknown error")
            for item in failed[:MAX_LISTED_FAILURES]
        ],
    )
