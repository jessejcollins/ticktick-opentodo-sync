"""One test per row of the sync rules table (CLAUDE.md), plus guard/match helpers."""

from __future__ import annotations

from dataclasses import replace

from ticktick_opentodo_sync.engine import (
    Action,
    Kind,
    Pair,
    Snapshot,
    destructive_count,
    match_existing,
    plan,
)
from ticktick_opentodo_sync.models import Task

T = Task(title="Buy paint", project="p1", notes="green")


def pair(tt: Task = T, ot: Task = T, base: Task | None = T) -> Pair:
    return Pair("tt1", "ot1", tt.content_hash(), ot.content_hash(), "p1", base)


def kinds(actions: list[Action]) -> list[Kind]:
    return [a.kind for a in actions]


def test_new_in_ticktick_creates_in_opentodo() -> None:
    acts = plan([], Snapshot(tasks={"tt1": T}), Snapshot())
    assert kinds(acts) == [Kind.CREATE_OT]
    assert acts[0].tt_id == "tt1" and acts[0].task == T and acts[0].tt_hash == T.content_hash()


def test_new_in_opentodo_creates_in_ticktick() -> None:
    acts = plan([], Snapshot(), Snapshot(tasks={"ot1": T}))
    assert kinds(acts) == [Kind.CREATE_TT]
    assert acts[0].ot_hash == T.content_hash()


def test_new_completed_tasks_are_not_created() -> None:
    done = replace(T, completed=True)
    assert plan([], Snapshot(tasks={"a": done}), Snapshot(tasks={"b": done})) == []


def test_unchanged_pair_does_nothing() -> None:
    assert plan([pair()], Snapshot(tasks={"tt1": T}), Snapshot(tasks={"ot1": T})) == []


def test_changed_only_in_ticktick_pushes_to_opentodo() -> None:
    new = replace(T, title="Buy more paint")
    acts = plan([pair()], Snapshot(tasks={"tt1": new}), Snapshot(tasks={"ot1": T}))
    assert kinds(acts) == [Kind.UPDATE_OT]
    assert acts[0].task == new and acts[0].tt_hash == new.content_hash() and not acts[0].conflict


def test_changed_only_in_opentodo_pushes_to_ticktick() -> None:
    new = replace(T, notes="dark green")
    acts = plan([pair()], Snapshot(tasks={"tt1": T}), Snapshot(tasks={"ot1": new}))
    assert kinds(acts) == [Kind.UPDATE_TT]
    assert acts[0].task == new and acts[0].ot_hash == new.content_hash()


def test_changed_in_both_ticktick_wins_and_logs_conflict() -> None:
    tt, ot = replace(T, title="TT title"), replace(T, title="OT title")
    acts = plan([pair()], Snapshot(tasks={"tt1": tt}), Snapshot(tasks={"ot1": ot}))
    assert kinds(acts) == [Kind.UPDATE_OT]
    assert acts[0].task == tt and acts[0].conflict


def test_same_change_on_both_sides_only_rehashes() -> None:
    new = replace(T, title="Same")
    acts = plan([pair()], Snapshot(tasks={"tt1": new}), Snapshot(tasks={"ot1": new}))
    assert kinds(acts) == [Kind.REHASH]


def test_completed_in_ticktick_completes_in_opentodo() -> None:
    done = replace(T, completed=True)
    acts = plan([pair()], Snapshot(tasks={"tt1": done}), Snapshot(tasks={"ot1": T}))
    assert kinds(acts) == [Kind.UPDATE_OT]
    assert acts[0].completes and acts[0].task == done


def test_completed_in_opentodo_completes_in_ticktick() -> None:
    done = replace(T, completed=True)
    acts = plan([pair()], Snapshot(tasks={"tt1": T}), Snapshot(tasks={"ot1": done}))
    assert kinds(acts) == [Kind.UPDATE_TT] and acts[0].completes


def test_conflict_keeps_completion_from_opentodo() -> None:
    tt = replace(T, title="Renamed in TT")
    ot = replace(T, completed=True)
    acts = plan([pair()], Snapshot(tasks={"tt1": tt}), Snapshot(tasks={"ot1": ot}))
    merged = replace(tt, completed=True)
    assert kinds(acts) == [Kind.UPDATE_OT, Kind.UPDATE_TT]
    assert all(a.task == merged for a in acts)
    # Both sides are written, so neither action may stamp the other side's (stale) hash.
    assert acts[0].tt_hash is None and acts[1].ot_hash is None


def test_deleted_in_ticktick_deletes_in_opentodo() -> None:
    acts = plan([pair()], Snapshot(deleted={"tt1"}), Snapshot(tasks={"ot1": T}))
    assert kinds(acts) == [Kind.DELETE_OT]


def test_deleted_in_ticktick_wins_even_if_opentodo_changed() -> None:
    acts = plan([pair()], Snapshot(deleted={"tt1"}), Snapshot(tasks={"ot1": replace(T, title="x")}))
    assert kinds(acts) == [Kind.DELETE_OT]


def test_deleted_in_opentodo_deletes_in_ticktick() -> None:
    acts = plan([pair()], Snapshot(tasks={"tt1": T}), Snapshot(deleted={"ot1"}))
    assert kinds(acts) == [Kind.DELETE_TT]


def test_deleted_in_opentodo_but_changed_in_ticktick_is_recreated() -> None:
    new = replace(T, title="Still wanted")
    acts = plan([pair()], Snapshot(tasks={"tt1": new}), Snapshot(deleted={"ot1"}))
    assert kinds(acts) == [Kind.CREATE_OT]
    assert acts[0].task == new and acts[0].conflict


def test_purged_in_opentodo_counts_as_deleted() -> None:
    acts = plan([pair()], Snapshot(tasks={"tt1": T}), Snapshot())
    assert kinds(acts) == [Kind.DELETE_TT]


def test_deleted_on_both_sides_forgets_pair() -> None:
    acts = plan([pair()], Snapshot(deleted={"tt1"}), Snapshot(deleted={"ot1"}))
    assert kinds(acts) == [Kind.FORGET]


def test_ticktick_unchanged_marker_uses_base() -> None:
    done = replace(T, completed=True)
    p = pair(tt=done, ot=done, base=done)
    assert plan([p], Snapshot(unchanged={"tt1"}), Snapshot(tasks={"ot1": done})) == []
    reopened = replace(done, completed=False)
    acts = plan([p], Snapshot(unchanged={"tt1"}), Snapshot(tasks={"ot1": reopened}))
    assert kinds(acts) == [Kind.UPDATE_TT] and not acts[0].task.completed  # type: ignore[union-attr]


def test_out_of_scope_pairs_are_left_alone() -> None:
    assert plan([pair()], Snapshot(out_of_scope={"tt1"}), Snapshot(deleted={"ot1"})) == []
    assert plan([pair()], Snapshot(deleted={"tt1"}), Snapshot(out_of_scope={"ot1"})) == []


def test_destructive_count() -> None:
    acts = [
        Action(Kind.DELETE_OT),
        Action(Kind.DELETE_TT),
        Action(Kind.UPDATE_OT, completes=True),
        Action(Kind.UPDATE_OT),
        Action(Kind.CREATE_OT),
    ]
    assert destructive_count(acts) == 3


def test_match_existing_pairs_unique_titles_only() -> None:
    tt = Snapshot(
        tasks={
            "a": T,
            "b": replace(T, title="dup"),
            "c": replace(T, title="dup"),
            "d": replace(T, title="only tt"),
        }
    )
    ot = Snapshot(
        tasks={
            "1": T,
            "2": replace(T, title="dup"),
            "3": replace(T, title="only ot"),
            "4": replace(T, title="Buy paint", project="other"),
        }
    )
    m = match_existing([], tt, ot)
    assert m.matches == [("a", "1")]
    assert sorted(m.tt_unmatched) == ["b", "c", "d"]
    assert sorted(m.ot_unmatched) == ["2", "3", "4"]
    assert m.ambiguous == [("p1", "dup")]
