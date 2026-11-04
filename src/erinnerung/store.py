"""Zustand: welche Erinnerung ist offen, wann wurde zuletzt genervt.

Eine SQLite-Datei. Die YAML sagt *was* wann fällig wird, hier steht, *wie es
ausging*. Der ``UNIQUE(reminder_id, event_at)``-Constraint ist das Rückgrat der
Neustart-Sicherheit: derselbe Zeitraum darf beliebig oft verarbeitet werden,
ohne dass doppelt erinnert wird.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import overload

OPEN = "open"
ACKED = "acked"
SKIPPED = "skipped"
SUPERSEDED = "superseded"  # erneut fällig geworden, alte Instanz weicht
EXPIRED = "expired"  # während einer Downtime durchgerutscht
REMOVED = "removed"  # aus der YAML gelöscht

RESOLVED_STATES = (ACKED, SKIPPED, SUPERSEDED, EXPIRED, REMOVED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS occurrences (
    id            INTEGER PRIMARY KEY,
    reminder_id   TEXT    NOT NULL,
    event_at      TEXT    NOT NULL,
    due_at        TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    resolved_at   TEXT,
    last_nag_at   TEXT,
    nag_count     INTEGER NOT NULL DEFAULT 0,
    snoozed_until TEXT,
    message_id    INTEGER,
    UNIQUE(reminder_id, event_at)
);
CREATE INDEX IF NOT EXISTS occurrences_status_due ON occurrences(status, due_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@overload
def _iso(moment: datetime) -> str: ...
@overload
def _iso(moment: None) -> None: ...
def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat()


@overload
def _parse(value: str) -> datetime: ...
@overload
def _parse(value: str | None) -> datetime | None: ...
def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True)
class Occurrence:
    """Eine konkrete Fälligkeit einer Erinnerung."""

    id: int
    reminder_id: str
    event_at: datetime
    due_at: datetime
    status: str
    title: str
    created_at: datetime
    resolved_at: datetime | None = None
    last_nag_at: datetime | None = None
    nag_count: int = 0
    snoozed_until: datetime | None = None
    message_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Occurrence:
        return cls(
            id=row["id"],
            reminder_id=row["reminder_id"],
            event_at=_parse(row["event_at"]),
            due_at=_parse(row["due_at"]),
            status=row["status"],
            title=row["title"],
            created_at=_parse(row["created_at"]),
            resolved_at=_parse(row["resolved_at"]),
            last_nag_at=_parse(row["last_nag_at"]),
            nag_count=row["nag_count"],
            snoozed_until=_parse(row["snoozed_until"]),
            message_id=row["message_id"],
        )

    @property
    def has_lead(self) -> bool:
        return self.event_at != self.due_at


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- Metadaten ---------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    def get_time(self, key: str) -> datetime | None:
        return _parse(self.get_meta(key))

    def set_time(self, key: str, moment: datetime) -> None:
        self.set_meta(key, _iso(moment))

    # -- Fälligkeiten ------------------------------------------------------

    def add_occurrence(
        self,
        *,
        reminder_id: str,
        title: str,
        event_at: datetime,
        due_at: datetime,
        status: str,
        now: datetime,
    ) -> Occurrence | None:
        """Legt eine Fälligkeit an. ``None``, wenn es sie schon gibt."""
        resolved = now if status in RESOLVED_STATES else None
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO occurrences"
            "(reminder_id, event_at, due_at, status, title, created_at, resolved_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (reminder_id, _iso(event_at), _iso(due_at), status, title, _iso(now), _iso(resolved)),
        )
        self._db.commit()
        if cursor.rowcount == 0 or cursor.lastrowid is None:
            return None
        return self.get(cursor.lastrowid)

    def get(self, occurrence_id: int) -> Occurrence | None:
        row = self._db.execute(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence_id,)
        ).fetchone()
        return Occurrence.from_row(row) if row else None

    def open_occurrences(self) -> list[Occurrence]:
        rows = self._db.execute(
            "SELECT * FROM occurrences WHERE status = ? ORDER BY due_at", (OPEN,)
        ).fetchall()
        return [Occurrence.from_row(row) for row in rows]

    def open_for_reminder(self, reminder_id: str) -> list[Occurrence]:
        rows = self._db.execute(
            "SELECT * FROM occurrences WHERE status = ? AND reminder_id = ? ORDER BY due_at",
            (OPEN, reminder_id),
        ).fetchall()
        return [Occurrence.from_row(row) for row in rows]

    def resolve(self, occurrence_id: int, status: str, now: datetime) -> None:
        self._db.execute(
            "UPDATE occurrences SET status = ?, resolved_at = ? WHERE id = ?",
            (status, _iso(now), occurrence_id),
        )
        self._db.commit()

    def supersede_others(self, reminder_id: str, keep_id: int, now: datetime) -> list[Occurrence]:
        """Ältere offene Instanzen derselben Erinnerung verfallen lassen."""
        rows = self._db.execute(
            "SELECT * FROM occurrences WHERE status = ? AND reminder_id = ? AND id != ?",
            (OPEN, reminder_id, keep_id),
        ).fetchall()
        outdated = [Occurrence.from_row(row) for row in rows]
        for occurrence in outdated:
            self.resolve(occurrence.id, SUPERSEDED, now)
        return outdated

    def record_nag(self, occurrence_id: int, now: datetime, message_id: int | None) -> None:
        self._db.execute(
            "UPDATE occurrences SET last_nag_at = ?, nag_count = nag_count + 1, message_id = ? "
            "WHERE id = ?",
            (_iso(now), message_id, occurrence_id),
        )
        self._db.commit()

    def snooze(self, occurrence_id: int, until: datetime) -> None:
        self._db.execute(
            "UPDATE occurrences SET snoozed_until = ? WHERE id = ?", (_iso(until), occurrence_id)
        )
        self._db.commit()

    def forget_message(self, occurrence_id: int) -> None:
        self._db.execute("UPDATE occurrences SET message_id = NULL WHERE id = ?", (occurrence_id,))
        self._db.commit()

    def counts_by_status(self) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT status, COUNT(*) AS n FROM occurrences GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def acked_count(self, reminder_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM occurrences WHERE reminder_id = ? AND status = ?",
            (reminder_id, ACKED),
        ).fetchone()
        return row["n"]

    def has_unresolved_or_history(self, reminder_id: str) -> bool:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM occurrences WHERE reminder_id = ?", (reminder_id,)
        ).fetchone()
        return row["n"] > 0
