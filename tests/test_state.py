from __future__ import annotations

from pathlib import Path

import pytest

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


def test_file_lock_excludes(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    with file_lock(lock, timeout=1), pytest.raises(TimeoutError), file_lock(lock, timeout=0.6):
        pass
