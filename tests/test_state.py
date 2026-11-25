from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fakes import deny_writes

from ticktick_opentodo_sync.engine import Pair
from ticktick_opentodo_sync.models import Task
from ticktick_opentodo_sync.state import State, file_lock


def test_pairs_round_trip(tmp_path: Path) -> None:
    s = State(tmp_path / "s.db")
    base = Task("x", "p", tags=frozenset({"a"}))
    s.put_pair(Pair("t1", "o1", "h1", "h2", "p", base))
    s.put_pair(Pair("t2", "o2", "h3", "h4", "p", None))
    assert s.pair_count() == 2
    got = {p.tt_id: p for p in s.pairs()}
    assert got["t1"].base == base and got["t2"].base is None

    s.update_pair("t1", ot_hash="new")
    p = next(p for p in s.pairs() if p.tt_id == "t1")
    assert (p.tt_hash, p.ot_hash) == ("h1", "new")

    # Re-pairing an ot_id to another tt_id frees the old row (recreate case).
    s.put_pair(Pair("t3", "o2", "a", "b", "p"))
    assert {p.tt_id for p in s.pairs()} == {"t1", "t3"}
    s.delete_pair("t1")
    assert s.pair_count() == 1
    s.close()

    # Persisted.
    assert State(tmp_path / "s.db").pair_count() == 1


def test_meta_and_projects(tmp_path: Path) -> None:
    s = State(tmp_path / "s.db")
    s.set_meta_json("blocked", {"count": 3})
    assert s.get_meta_json("blocked") == {"count": 3}
    s.set_meta("blocked", None)
    assert s.get_meta("blocked") is None
    s.add_project_pair("t", "o", "Work")
    s.add_project_pair("t", "o2", "Work")
    assert s.project_pairs() == {"t": "o2"}
    s.drop_project_pair("t")
    assert s.project_pairs() == {}


def test_no_write_when_meta_unchanged(tmp_path: Path) -> None:
    s = State(tmp_path / "s.db")
    s.set_meta("k", "v")
    before = s.db.total_changes
    s.set_meta("k", "v")
    s.set_meta("missing", None)
    assert s.db.total_changes == before


def test_check_writable(tmp_path: Path) -> None:
    s = State(tmp_path / "s.db")
    before = s.db.total_changes
    s.check_writable()
    s.check_writable()
    assert s.db.total_changes == before + 2  # a real write each time
    deny_writes(s)
    with pytest.raises(sqlite3.DatabaseError):
        s.check_writable()


def test_failed_commit_rolls_back(tmp_path: Path) -> None:
    """A failed COMMIT must not leave the loop's connection inside the transaction."""
    s = State(tmp_path / "s.db")

    def no_commit(action: int, arg1: str | None, *_: object) -> int:
        deny = action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
        return sqlite3.SQLITE_DENY if deny else sqlite3.SQLITE_OK

    s.db.set_authorizer(no_commit)
    with pytest.raises(sqlite3.DatabaseError):
        s.put_pair(Pair("t", "o", "a", "b", "p"))
    assert not s.db.in_transaction
    s.db.set_authorizer(None)
    assert s.pair_count() == 0


def test_file_lock_excludes(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    with file_lock(lock, timeout=1), pytest.raises(TimeoutError), file_lock(lock, timeout=0.6):
        pass
