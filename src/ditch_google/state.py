"""Resumable state, stored as a SQLite ledger.

A migration can run for days and will be interrupted - by a dropped connection, a full
disk, a closed laptop. Every unit of work therefore records what stage it reached, so a
re-run picks up where it stopped instead of re-transferring gigabytes or, worse, uploading
duplicates.

Two levels of granularity:

* **Archives** move ``pending -> fetched -> unpacked -> completed``. This drives the
  streaming loop, which handles one archive at a time so peak disk stays bounded.
* **Items** (individual photos and videos) move ``discovered -> fixed -> uploaded ->
  verified``, with ``failed`` and ``skipped`` as terminal states.

Nothing is ever silently dropped: an item we could not match to a sidecar, or could not
upload, stays in the ledger with its error, and turns up in the final report.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from ditch_google.constants import APP_NAME

__all__ = [
    "Album",
    "Archive",
    "ArchiveStage",
    "Item",
    "ItemStage",
    "State",
    "default_state_path",
]


class ArchiveStage(StrEnum):
    """Where a single Takeout archive has got to."""

    PENDING = "pending"
    FETCHED = "fetched"
    UNPACKED = "unpacked"
    COMPLETED = "completed"
    FAILED = "failed"


class ItemStage(StrEnum):
    """Where a single photo or video has got to."""

    DISCOVERED = "discovered"
    FIXED = "fixed"
    UPLOADED = "uploaded"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Stages meaning "no further work needed".
TERMINAL_ITEM_STAGES: frozenset[ItemStage] = frozenset(
    {ItemStage.VERIFIED, ItemStage.FAILED, ItemStage.SKIPPED}
)


@dataclass(frozen=True)
class Archive:
    """One part of a Takeout export."""

    name: str
    remote_path: str
    size_bytes: int | None
    stage: ArchiveStage
    error: str | None
    updated_at: str


@dataclass(frozen=True)
class Item:
    """One photo or video."""

    id: int
    archive: str
    source_path: str
    sidecar_path: str | None
    sha1: str | None
    stage: ItemStage
    proton_uid: str | None
    error: str | None
    updated_at: str


@dataclass(frozen=True)
class Album:
    """A Takeout album, and its Proton counterpart once created."""

    id: int
    title: str
    proton_uid: str | None


# Applied in order; the database's `user_version` records how many have run. Never edit a
# migration that has shipped - append a new one instead.
_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE archives (
        name        TEXT PRIMARY KEY,
        remote_path TEXT NOT NULL,
        size_bytes  INTEGER,
        stage       TEXT NOT NULL DEFAULT 'pending',
        error       TEXT,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE items (
        id           INTEGER PRIMARY KEY,
        archive      TEXT NOT NULL REFERENCES archives(name) ON DELETE CASCADE,
        source_path  TEXT NOT NULL UNIQUE,
        sidecar_path TEXT,
        sha1         TEXT,
        stage        TEXT NOT NULL DEFAULT 'discovered',
        proton_uid   TEXT,
        error        TEXT,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX idx_items_stage ON items(stage);
    CREATE INDEX idx_items_archive ON items(archive);

    CREATE TABLE albums (
        id         INTEGER PRIMARY KEY,
        title      TEXT NOT NULL UNIQUE,
        proton_uid TEXT
    );

    CREATE TABLE album_items (
        album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        added    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (album_id, item_id)
    );
    """,
    # A stable identity for the migration, so a run stays findable after the fact - in
    # the files themselves (stamped into XMP) and in Proton (as a marker album).
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
)


def default_state_path() -> Path:
    """Where the ledger lives, honouring ``XDG_STATE_HOME``."""
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / APP_NAME / "photos.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State:
    """The migration ledger.

    Use as a context manager::

        with State.open() as state:
            state.add_archive("takeout-001.tgz", "drive:Takeout/takeout-001.tgz")
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls, path: Path | None = None) -> Self:
        """Open (creating if needed) the ledger at ``path``."""
        target = path or default_state_path()
        target.parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(target, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL survives a crash mid-write and lets `status` read while a migration runs.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")

        state = cls(connection)
        state._migrate()
        return state

    def _migrate(self) -> None:
        """Apply any migrations this database has not seen yet.

        The version bump is wrapped into the same script as the schema change, inside an
        explicit transaction, so the two commit together. Doing it as a separate statement
        would leave a crash in between able to re-run a migration that had already
        applied - which then fails on CREATE TABLE and bricks the ledger.
        """
        current: int = self._db.execute("PRAGMA user_version").fetchone()[0]
        for index, script in enumerate(_MIGRATIONS[current:], start=current):
            self._db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {index + 1};\nCOMMIT;")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Batch writes into one transaction.

        Worth using for bulk inserts: committing per row across a 100,000-photo library
        turns seconds of work into minutes.
        """
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    # ----------------------------------------------------------------------- meta

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def migration_id(self) -> str:
        """A short, stable identity for this migration, created on first use.

        Stamped into the files and used to name the marker album, so a run remains
        findable afterwards even without this database.
        """
        existing = self.get_meta("migration_id")
        if existing is not None:
            return existing
        created = uuid4().hex[:12]
        self.set_meta("migration_id", created)
        self.set_meta("started_at", _now())
        return created

    @property
    def started_at(self) -> str:
        """When the migration first ran. Triggers creation of the id if not yet set."""
        _ = self.migration_id
        return self.get_meta("started_at") or _now()

    # ------------------------------------------------------------------- archives

    def add_archive(self, name: str, remote_path: str, size_bytes: int | None = None) -> None:
        """Record an archive. Idempotent: re-recording never resets progress."""
        self._db.execute(
            """
            INSERT INTO archives (name, remote_path, size_bytes, stage, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                remote_path = excluded.remote_path,
                size_bytes  = COALESCE(excluded.size_bytes, archives.size_bytes)
            """,
            (name, remote_path, size_bytes, ArchiveStage.PENDING, _now()),
        )

    def set_archive_stage(self, name: str, stage: ArchiveStage, error: str | None = None) -> None:
        self._db.execute(
            "UPDATE archives SET stage = ?, error = ?, updated_at = ? WHERE name = ?",
            (stage, error, _now(), name),
        )

    def get_archive(self, name: str) -> Archive | None:
        row = self._db.execute("SELECT * FROM archives WHERE name = ?", (name,)).fetchone()
        return _to_archive(row) if row else None

    def archives(self, stage: ArchiveStage | None = None) -> list[Archive]:
        if stage is None:
            rows = self._db.execute("SELECT * FROM archives ORDER BY name").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM archives WHERE stage = ? ORDER BY name", (stage,)
            ).fetchall()
        return [_to_archive(row) for row in rows]

    def next_archive(self) -> Archive | None:
        """The next archive needing work, or ``None`` when the run is complete.

        Failed archives are not retried automatically - a re-run would likely fail the
        same way, and burying the error behind an infinite retry helps nobody.
        """
        row = self._db.execute(
            """
            SELECT * FROM archives
            WHERE stage NOT IN (?, ?)
            ORDER BY name LIMIT 1
            """,
            (ArchiveStage.COMPLETED, ArchiveStage.FAILED),
        ).fetchone()
        return _to_archive(row) if row else None

    # ---------------------------------------------------------------------- items

    def add_item(self, archive: str, source_path: str, sidecar_path: str | None = None) -> int:
        """Record a media file, returning its id.

        Idempotent on ``source_path``: re-running discovery over an already-unpacked
        archive must not duplicate rows or reset an item's stage.
        """
        self._db.execute(
            """
            INSERT INTO items (archive, source_path, sidecar_path, stage, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
                sidecar_path = COALESCE(excluded.sidecar_path, items.sidecar_path)
            """,
            (archive, source_path, sidecar_path, ItemStage.DISCOVERED, _now()),
        )
        row = self._db.execute(
            "SELECT id FROM items WHERE source_path = ?", (source_path,)
        ).fetchone()
        return int(row["id"])

    def add_items(self, archive: str, entries: Iterable[tuple[str, str | None]]) -> int:
        """Bulk-record media files. Returns how many rows were seen."""
        rows = [
            (archive, source, sidecar, ItemStage.DISCOVERED, _now()) for source, sidecar in entries
        ]
        with self.transaction():
            self._db.executemany(
                """
                INSERT INTO items (archive, source_path, sidecar_path, stage, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    sidecar_path = COALESCE(excluded.sidecar_path, items.sidecar_path)
                """,
                rows,
            )
        return len(rows)

    def set_item_stage(
        self,
        source_path: str,
        stage: ItemStage,
        *,
        error: str | None = None,
        proton_uid: str | None = None,
        sha1: str | None = None,
    ) -> None:
        """Advance an item, optionally attaching its Proton identity or checksum.

        ``proton_uid`` and ``sha1`` are only overwritten when supplied, so advancing an
        item through later stages never discards what an earlier one recorded.
        """
        self._db.execute(
            """
            UPDATE items SET
                stage      = ?,
                error      = ?,
                proton_uid = COALESCE(?, proton_uid),
                sha1       = COALESCE(?, sha1),
                updated_at = ?
            WHERE source_path = ?
            """,
            (stage, error, proton_uid, sha1, _now(), source_path),
        )

    def get_item(self, source_path: str) -> Item | None:
        row = self._db.execute(
            "SELECT * FROM items WHERE source_path = ?", (source_path,)
        ).fetchone()
        return _to_item(row) if row else None

    def items(
        self,
        *,
        stage: ItemStage | None = None,
        archive: str | None = None,
    ) -> list[Item]:
        clauses: list[str] = []
        params: list[object] = []
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        if archive is not None:
            clauses.append("archive = ?")
            params.append(archive)
        # The interpolated text is built from the fixed literals above, never from caller
        # input. Every value is passed as a bound parameter.
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM items {where} ORDER BY id"  # noqa: S608
        rows = self._db.execute(sql, params).fetchall()
        return [_to_item(row) for row in rows]

    def pending_items(self, archive: str | None = None) -> list[Item]:
        """Items still needing work - the basis of resume."""
        params: list[object] = list(TERMINAL_ITEM_STAGES)
        # Only `?` placeholders are interpolated - one per terminal stage. The stage
        # values themselves are bound parameters.
        placeholders = ", ".join("?" for _ in TERMINAL_ITEM_STAGES)
        sql = f"SELECT * FROM items WHERE stage NOT IN ({placeholders})"  # noqa: S608
        if archive is not None:
            sql += " AND archive = ?"
            params.append(archive)
        rows = self._db.execute(f"{sql} ORDER BY id", params).fetchall()
        return [_to_item(row) for row in rows]

    def items_without_sidecar(self) -> list[Item]:
        """Media we could not pair with a sidecar - reported, never silently skipped."""
        rows = self._db.execute(
            "SELECT * FROM items WHERE sidecar_path IS NULL ORDER BY id"
        ).fetchall()
        return [_to_item(row) for row in rows]

    # --------------------------------------------------------------------- albums

    def add_album(self, title: str, proton_uid: str | None = None) -> int:
        """Record an album, returning its id. Idempotent on title.

        Album creation must be idempotent or a resumed run creates a second "Holiday
        2019" in Proton alongside the first.
        """
        self._db.execute(
            """
            INSERT INTO albums (title, proton_uid) VALUES (?, ?)
            ON CONFLICT(title) DO UPDATE SET
                proton_uid = COALESCE(excluded.proton_uid, albums.proton_uid)
            """,
            (title, proton_uid),
        )
        row = self._db.execute("SELECT id FROM albums WHERE title = ?", (title,)).fetchone()
        return int(row["id"])

    def set_album_uid(self, title: str, proton_uid: str) -> None:
        self._db.execute("UPDATE albums SET proton_uid = ? WHERE title = ?", (proton_uid, title))

    def get_album(self, title: str) -> Album | None:
        row = self._db.execute("SELECT * FROM albums WHERE title = ?", (title,)).fetchone()
        return _to_album(row) if row else None

    def albums(self) -> list[Album]:
        rows = self._db.execute("SELECT * FROM albums ORDER BY title").fetchall()
        return [_to_album(row) for row in rows]

    def link_item_to_album(self, album_id: int, item_id: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO album_items (album_id, item_id) VALUES (?, ?)",
            (album_id, item_id),
        )

    def mark_album_item_added(self, album_id: int, item_id: int) -> None:
        """Record that the photo is now in the Proton album."""
        self._db.execute(
            "UPDATE album_items SET added = 1 WHERE album_id = ? AND item_id = ?",
            (album_id, item_id),
        )

    def album_members(self, album_id: int, *, pending_only: bool = False) -> list[Item]:
        """Items belonging to an album; optionally only those not yet added in Proton."""
        sql = """
            SELECT items.* FROM items
            JOIN album_items ON album_items.item_id = items.id
            WHERE album_items.album_id = ?
        """
        if pending_only:
            sql += " AND album_items.added = 0"
        rows = self._db.execute(f"{sql} ORDER BY items.id", (album_id,)).fetchall()
        return [_to_item(row) for row in rows]

    # -------------------------------------------------------------------- summary

    def item_counts(self) -> dict[str, int]:
        """Item totals per stage, including stages with no items."""
        counts = dict.fromkeys((stage.value for stage in ItemStage), 0)
        for row in self._db.execute("SELECT stage, COUNT(*) AS n FROM items GROUP BY stage"):
            counts[str(row["stage"])] = int(row["n"])
        return counts

    def archive_counts(self) -> dict[str, int]:
        """Archive totals per stage, including stages with no archives."""
        counts = dict.fromkeys((stage.value for stage in ArchiveStage), 0)
        for row in self._db.execute("SELECT stage, COUNT(*) AS n FROM archives GROUP BY stage"):
            counts[str(row["stage"])] = int(row["n"])
        return counts

    def is_complete(self) -> bool:
        """True when no archive and no item still needs work."""
        return self.next_archive() is None and not self.pending_items()


def _to_archive(row: sqlite3.Row) -> Archive:
    return Archive(
        name=row["name"],
        remote_path=row["remote_path"],
        size_bytes=row["size_bytes"],
        stage=ArchiveStage(row["stage"]),
        error=row["error"],
        updated_at=row["updated_at"],
    )


def _to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        archive=row["archive"],
        source_path=row["source_path"],
        sidecar_path=row["sidecar_path"],
        sha1=row["sha1"],
        stage=ItemStage(row["stage"]),
        proton_uid=row["proton_uid"],
        error=row["error"],
        updated_at=row["updated_at"],
    )


def _to_album(row: sqlite3.Row) -> Album:
    return Album(id=row["id"], title=row["title"], proton_uid=row["proton_uid"])
