"""SQLite state: task and checklist-item id pairs with per-side hashes, the project map, meta.

Every write is its own short transaction, issued only when something changed.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .engine import Pair
from .habits import HabitDef, HabitPair
from .models import Task

SCHEMA_VERSION = 4

TASKS = "pairs"
ITEMS = "item_pairs"
"""TickTick checklist item <-> OpenTodo subtask; ``tt_project`` is the TickTick parent task id."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    tt_id TEXT PRIMARY KEY,
    ot_id TEXT NOT NULL UNIQUE,
    name  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairs (
    tt_id      TEXT PRIMARY KEY,
    ot_id      TEXT NOT NULL UNIQUE,
    tt_hash    TEXT NOT NULL,
    ot_hash    TEXT NOT NULL,
    tt_project TEXT NOT NULL,
    base       TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_pairs (
    tt_id      TEXT PRIMARY KEY,
    ot_id      TEXT NOT NULL UNIQUE,
    tt_hash    TEXT NOT NULL,
    ot_hash    TEXT NOT NULL,
    base       TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_days (
    tt_id TEXT NOT NULL,
    day   TEXT NOT NULL,
    PRIMARY KEY (tt_id, day)
);
CREATE TABLE IF NOT EXISTS habit_refused (
    tt_id TEXT NOT NULL,
    day   TEXT NOT NULL,
    PRIMARY KEY (tt_id, day)
);
CREATE TABLE IF NOT EXISTS item_pairs (
    tt_id      TEXT PRIMARY KEY,
    ot_id      TEXT NOT NULL UNIQUE,
    tt_hash    TEXT NOT NULL,
    ot_hash    TEXT NOT NULL,
    tt_project TEXT NOT NULL,
    base       TEXT,
    updated_at INTEGER NOT NULL
);
"""


class State:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if self._user_version() < SCHEMA_VERSION:
            self.db.executescript(
                f"BEGIN;\n{_SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )

    def close(self) -> None:
        self.db.close()

    def _user_version(self) -> int:
        return int(self.db.execute("PRAGMA user_version").fetchone()[0])

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.db.execute("BEGIN")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            # A failed COMMIT can leave the transaction open (and SQLite may already have
            # rolled back after a disk error): never leave the loop's connection inside one.
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def check_writable(self) -> None:
        """Prove the state can be written right now; raises ``sqlite3.Error`` if not.

        A pass calls this before it writes to either app. A task created in one app whose
        pair can't be recorded is created again by every later pass, and its copy copied
        back, so the duplicates double each pass. One small write per pass that writes
        anyway (a fresh value, so it reaches the disk: an unchanged row isn't rewritten).
        """
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES ('write_check', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(time.time_ns()),),
        )

    # meta ------------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str | None) -> None:
        """Set or (with ``None``) delete; no-op when unchanged."""
        if self.get_meta(key) == value:
            return
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta_json(self, key: str) -> Any:
        raw = self.get_meta(key)
        return None if raw is None else json.loads(raw)

    def set_meta_json(self, key: str, value: Any) -> None:
        self.set_meta(key, None if value is None else json.dumps(value, sort_keys=True))

    # projects --------------------------------------------------------------------------

    def project_pairs(self) -> dict[str, str]:
        return {
            r["tt_id"]: r["ot_id"] for r in self.db.execute("SELECT tt_id, ot_id FROM projects")
        }

    def add_project_pair(self, tt_id: str, ot_id: str, name: str) -> None:
        self.db.execute(
            "INSERT INTO projects (tt_id, ot_id, name) VALUES (?, ?, ?) "
            "ON CONFLICT(tt_id) DO UPDATE SET ot_id = excluded.ot_id, name = excluded.name",
            (tt_id, ot_id, name),
        )

    def drop_project_pair(self, tt_id: str) -> None:
        self.db.execute("DELETE FROM projects WHERE tt_id = ?", (tt_id,))

    # task pairs ------------------------------------------------------------------------

    def pairs(self, table: str = TASKS) -> list[Pair]:
        return [
            Pair(
                tt_id=r["tt_id"],
                ot_id=r["ot_id"],
                tt_hash=r["tt_hash"],
                ot_hash=r["ot_hash"],
                tt_project=r["tt_project"],
                base=Task.from_json(json.loads(r["base"])) if r["base"] else None,
            )
            for r in self.db.execute(f"SELECT * FROM {_table(table)}")
        ]

    def pair_count(self, table: str = TASKS) -> int:
        return int(self.db.execute(f"SELECT COUNT(*) FROM {_table(table)}").fetchone()[0])

    def put_pair(self, pair: Pair, table: str = TASKS) -> None:
        """Insert or replace the pair keyed by ``tt_id`` (also frees a reused ``ot_id``)."""
        with self.tx():
            self.db.execute(
                f"DELETE FROM {_table(table)} WHERE ot_id = ? AND tt_id != ?",
                (pair.ot_id, pair.tt_id),
            )
            self.db.execute(
                f"INSERT OR REPLACE INTO {_table(table)} "
                "(tt_id, ot_id, tt_hash, ot_hash, tt_project, base, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    pair.tt_id,
                    pair.ot_id,
                    pair.tt_hash,
                    pair.ot_hash,
                    pair.tt_project,
                    _dump(pair.base),
                    int(time.time()),
                ),
            )

    def update_pair(
        self,
        tt_id: str,
        *,
        tt_hash: str | None = None,
        ot_hash: str | None = None,
        tt_project: str | None = None,
        base: Task | None = None,
        table: str = TASKS,
    ) -> None:
        sets: dict[str, object] = {
            k: v
            for k, v in (("tt_hash", tt_hash), ("ot_hash", ot_hash), ("tt_project", tt_project))
            if v is not None
        }
        if base is not None:
            sets["base"] = _dump(base)
        if not sets:
            return
        sets["updated_at"] = int(time.time())
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.db.execute(
            f"UPDATE {_table(table)} SET {cols} WHERE tt_id = ?", (*sets.values(), tt_id)
        )

    def delete_pair(self, tt_id: str, table: str = TASKS) -> None:
        self.db.execute(f"DELETE FROM {_table(table)} WHERE tt_id = ?", (tt_id,))

    # checklist items -------------------------------------------------------------------

    def resync_items_from_ticktick(self, tt_parent: str) -> None:
        """Mark a parent's items changed on both sides, so TickTick's state wins next pass."""
        self.db.execute(
            "UPDATE item_pairs SET tt_hash = '', ot_hash = '' WHERE tt_project = ?", (tt_parent,)
        )

    def drop_orphan_items(self) -> int:
        """Forget item pairs whose parent task pair is gone."""
        cur = self.db.execute(
            "DELETE FROM item_pairs WHERE tt_project NOT IN (SELECT tt_id FROM pairs)"
        )
        return cur.rowcount


# habits (module level so State stays one class of short methods) ---------------------------


def habit_pairs(state: State) -> list[HabitPair]:
    return [
        HabitPair(
            tt_id=r["tt_id"],
            ot_id=r["ot_id"],
            tt_hash=r["tt_hash"],
            ot_hash=r["ot_hash"],
            base=HabitDef.from_json(json.loads(r["base"])) if r["base"] else None,
        )
        for r in state.db.execute("SELECT * FROM habit_pairs")
    ]


def put_habit_pair(state: State, pair: HabitPair) -> None:
    with state.tx():
        state.db.execute(
            "DELETE FROM habit_pairs WHERE ot_id = ? AND tt_id != ?", (pair.ot_id, pair.tt_id)
        )
        state.db.execute(
            "INSERT OR REPLACE INTO habit_pairs "
            "(tt_id, ot_id, tt_hash, ot_hash, base, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                pair.tt_id,
                pair.ot_id,
                pair.tt_hash,
                pair.ot_hash,
                None if pair.base is None else json.dumps(pair.base.to_json(), sort_keys=True),
                int(time.time()),
            ),
        )


def habit_days(state: State) -> set[tuple[str, str]]:
    """(TickTick habit id, day) checked in on both sides at the last sync."""
    return {(r["tt_id"], r["day"]) for r in state.db.execute("SELECT tt_id, day FROM habit_days")}


def set_habit_day(state: State, tt_id: str, day: str, done: bool) -> None:
    if done:
        state.db.execute(
            "INSERT OR IGNORE INTO habit_days (tt_id, day) VALUES (?, ?)", (tt_id, day)
        )
    else:
        state.db.execute("DELETE FROM habit_days WHERE tt_id = ? AND day = ?", (tt_id, day))


def habit_refused(state: State) -> set[tuple[str, str]]:
    """(TickTick habit id, day) check-ins OpenTodo refused to record; kept in TickTick only."""
    return {
        (r["tt_id"], r["day"]) for r in state.db.execute("SELECT tt_id, day FROM habit_refused")
    }


def set_habit_refused(state: State, tt_id: str, day: str, refused: bool) -> None:
    if refused:
        state.db.execute(
            "INSERT OR IGNORE INTO habit_refused (tt_id, day) VALUES (?, ?)", (tt_id, day)
        )
    else:
        state.db.execute("DELETE FROM habit_refused WHERE tt_id = ? AND day = ?", (tt_id, day))


def prune_habit_days(state: State, before: str) -> None:
    """Forget days that left the sync window (only when there are any: SD card)."""
    if state.db.execute("SELECT 1 FROM habit_days WHERE day < ? LIMIT 1", (before,)).fetchone():
        state.db.execute("DELETE FROM habit_days WHERE day < ?", (before,))


def _table(name: str) -> str:
    if name not in (TASKS, ITEMS):
        raise ValueError(f"unknown pair table {name!r}")
    return name


def _dump(task: Task | None) -> str | None:
    return None if task is None else json.dumps(task.to_json(), sort_keys=True)


@contextmanager
def file_lock(path: Path, *, timeout: float) -> Iterator[None]:
    """Exclusive advisory lock shared by the loop container and one-off runs."""
    with path.open("a") as fh:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another ticktick-opentodo-sync run holds {path}") from None
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
