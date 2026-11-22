"""SQLite state: task id pairs with per-side hashes, the project map, and a few meta keys.

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
from .models import Task

SCHEMA_VERSION = 1

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
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

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

    def pairs(self) -> list[Pair]:
        return [
            Pair(
                tt_id=r["tt_id"],
                ot_id=r["ot_id"],
                tt_hash=r["tt_hash"],
                ot_hash=r["ot_hash"],
                tt_project=r["tt_project"],
                base=Task.from_json(json.loads(r["base"])) if r["base"] else None,
            )
            for r in self.db.execute("SELECT * FROM pairs")
        ]

    def pair_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0])

    def put_pair(self, pair: Pair) -> None:
        """Insert or replace the pair keyed by ``tt_id`` (also frees a reused ``ot_id``)."""
        with self.tx():
            self.db.execute(
                "DELETE FROM pairs WHERE ot_id = ? AND tt_id != ?", (pair.ot_id, pair.tt_id)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO pairs "
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
        self.db.execute(f"UPDATE pairs SET {cols} WHERE tt_id = ?", (*sets.values(), tt_id))

    def delete_pair(self, tt_id: str) -> None:
        self.db.execute("DELETE FROM pairs WHERE tt_id = ?", (tt_id,))


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
