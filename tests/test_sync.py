"""End-to-end passes against in-memory fakes of both APIs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeOpenTodo, FakeTickTick, deny_writes

from ticktick_opentodo_sync.opentodo import PLACEHOLDER
from ticktick_opentodo_sync.state import State
from ticktick_opentodo_sync.sync import PassResult, Syncer

NY = "America/New_York"


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tt = FakeTickTick()
        self.ot = FakeOpenTodo()
        self.state = State(tmp / "state.db")
        self.max_destructive = 10

    def run(self, *, dry_run: bool = False, allow_bulk: bool = False) -> PassResult:
        syncer = Syncer(self.tt, self.ot, self.state, max_destructive=self.max_destructive)
        return syncer.run(dry_run=dry_run, allow_bulk=allow_bulk)

    def assert_quiet(self) -> None:
        """A follow-up pass must not write anything (loop prevention)."""
        tt_w, ot_w = len(self.tt.writes), len(self.ot.writes)
        res = self.run()
        assert not res.changed and res.ok, res.summary()
        assert (len(self.tt.writes), len(self.ot.writes)) == (tt_w, ot_w)

    def ot_by_title(self, title: str) -> dict[str, Any]:
        return next(t for t in self.ot.tasks_.values() if t["title"] == title)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.state.close()


def seed(env: Env) -> tuple[str, str]:
    work = env.tt.add_project("Work")
    a = env.tt.add_task("Inbox thing")
    b = env.tt.add_task(
        "Report",
        work,
        content="Q3 numbers",
        priority=5,
        tags=["Office"],
        dueDate="2026-11-25T14:00:00+0000",
        isAllDay=False,
        timeZone=NY,
    )
    res = env.run()
    assert res.ok and res.ot.created == 2, res.summary()
    return a, b


def test_seed_creates_everything_in_opentodo(env: Env) -> None:
    seed(env)
    assert [p["name"] for p in env.ot.projects_.values()] == ["Work"]
    inbox = env.ot_by_title("Inbox thing")
    report = env.ot_by_title("Report")
    assert inbox["project_id"] is None
    assert report["project_id"] == next(iter(env.ot.projects_))
    assert report["priority"] == 1
    assert report["description"] == "Q3 numbers"
    assert report["due"] == {"date": "2026-11-25", "time": "09:00", "tz": NY}
    assert [env.ot.label_name(x) for x in report["labels"]] == ["office"]
    assert env.state.pair_count() == 2
    assert env.state.get_meta("tt_inbox_id") == "inbox100"
    env.assert_quiet()


def test_placeholder_project_name_is_repaired(env: Env) -> None:
    """A browser saved the create placeholder over a paired project's name."""
    seed(env)
    pid = next(iter(env.ot.projects_))
    env.ot.projects_[pid]["name"] = PLACEHOLDER
    env.ot.add_project(PLACEHOLDER)  # not paired: left alone
    env.run()
    assert sorted(p["name"] for p in env.ot.projects_.values()) == sorted(["Work", PLACEHOLDER])
    assert env.ot_by_title("Report")["project_id"] == pid
    assert [p["name"] for p in env.tt.projects_.values()] == ["Work"]
    env.assert_quiet()


def test_dry_run_writes_nothing(env: Env) -> None:
    env.tt.add_task("x", env.tt.add_project("Work"))
    res = env.run(dry_run=True)
    assert res.ot.created == 1
    assert env.ot.writes == [] and env.tt.writes == []
    assert env.state.pair_count() == 0 and env.state.project_pairs() == {}


def test_new_opentodo_task_goes_to_ticktick(env: Env) -> None:
    seed(env)
    otp = next(iter(env.ot.projects_))
    env.ot.add_task("From OT", otp, priority=2, due={"date": "2026-12-01"})
    res = env.run()
    assert res.tt.created == 1
    t = next(t for t in env.tt.tasks.values() if t["title"] == "From OT")
    assert env.tt.projects_[t["projectId"]]["name"] == "Work"
    assert t["priority"] == 3 and t["isAllDay"] is True
    env.assert_quiet()


def test_new_opentodo_project_is_created_in_ticktick(env: Env) -> None:
    seed(env)
    env.ot.add_task("Weed", env.ot.add_project("Garden"))
    env.run()
    garden = next(p for p in env.tt.projects_.values() if p["name"] == "Garden")
    assert any(t["projectId"] == garden["id"] for t in env.tt.tasks.values())
    env.assert_quiet()


def _delete_list(env: Env, n_tasks: int) -> str:
    """A synced TickTick list "Work" with ``n_tasks`` tasks, then deleted (with its tasks)."""
    work = env.tt.add_project("Work")
    tids = [env.tt.add_task(f"task {i}", work) for i in range(n_tasks)]
    env.run()
    del env.tt.projects_[work]
    for tid in tids:
        del env.tt.tasks[tid]
    return next(iter(env.ot.projects_))


def test_list_deleted_in_ticktick_archives_its_project(env: Env) -> None:
    """It used to come back: the OpenTodo project, now unpaired, was created as a new list."""
    otp = _delete_list(env, 2)
    env.ot.add_task("Draft", otp)  # added in OpenTodo since the last pass
    res = env.run()
    assert res.ok and res.ot.deleted == 2 and not res.tt.created, res.summary()
    assert env.ot.projects_[otp]["archived"] is True
    assert env.state.project_pairs() == {}
    assert not env.tt.projects_  # not recreated
    assert env.ot_by_title("Draft")["deleted_at"] is None  # left in the archived project
    env.assert_quiet()


def test_blocked_pass_writes_no_list_or_project_either(env: Env) -> None:
    """Lists and projects were created while collecting, before the guards ran."""
    otp = _delete_list(env, 12)
    writes = (len(env.tt.writes), len(env.ot.writes))
    res = env.run()
    assert res.blocked
    assert (len(env.tt.writes), len(env.ot.writes)) == writes
    res = env.run(allow_bulk=True)
    assert res.ok and res.ot.deleted == 12, res.summary()
    assert env.ot.projects_[otp]["archived"] is True and not env.tt.projects_
    env.assert_quiet()


def test_project_deleted_in_opentodo_is_restored(env: Env) -> None:
    """Deleting a project in OpenTodo only hides its tasks: TickTick wins, it comes back."""
    seed(env)
    otp = next(iter(env.ot.projects_))
    env.ot.projects_[otp]["deleted_at"] = 1
    res = env.run()
    assert res.ok and not res.tt.deleted and not res.ot.created, res.summary()
    assert env.ot.projects_[otp]["deleted_at"] is None
    assert env.ot_by_title("Report")["project_id"] == otp
    env.assert_quiet()


def test_project_purged_in_opentodo_is_recreated(env: Env) -> None:
    """Purging also purges its tasks: deleted in TickTick (bulk-guarded), the list stays."""
    seed(env)
    otp = next(iter(env.ot.projects_))
    del env.ot.projects_[otp]
    env.ot.tasks_ = {k: t for k, t in env.ot.tasks_.items() if t["project_id"] != otp}
    res = env.run()
    assert res.ok and res.tt.deleted == 1, res.summary()
    assert [p["name"] for p in env.ot.projects_.values()] == ["Work"]
    assert [p["name"] for p in env.tt.projects_.values()] == ["Work"]
    env.assert_quiet()


def test_new_list_never_pairs_with_an_archived_project(env: Env) -> None:
    """Its tasks would never sync: an archived project is out of scope."""
    old = env.ot.add_project("Groceries", archived=True)
    env.tt.add_task("milk", env.tt.add_project("Groceries"))
    env.run()
    milk = env.ot_by_title("milk")
    assert milk["project_id"] != old
    assert env.ot.projects_[milk["project_id"]]["name"] == "Groceries"
    assert env.ot.projects_[old]["archived"] is True
    env.assert_quiet()


def test_edit_in_opentodo_pushes_to_ticktick(env: Env) -> None:
    _, b = seed(env)
    env.ot_by_title("Report")["title"] = "Report v2"
    res = env.run()
    assert res.tt.updated == 1
    assert env.tt.tasks[b]["title"] == "Report v2"
    assert env.tt.tasks[b]["timeZone"] == NY  # untouched fields survive the merge
    env.assert_quiet()


def test_edit_in_ticktick_pushes_to_opentodo(env: Env) -> None:
    _, b = seed(env)
    env.tt.tasks[b]["tags"] = ["office", "urgent"]
    res = env.run()
    assert res.ot.updated == 1
    names = sorted(env.ot.label_name(x) for x in env.ot_by_title("Report")["labels"])
    assert names == ["office", "urgent"]
    env.assert_quiet()


def test_conflict_ticktick_wins(env: Env) -> None:
    _, b = seed(env)
    env.tt.tasks[b]["title"] = "TT wins"
    env.ot_by_title("Report")["description"] = "OT edit"
    res = env.run()
    assert res.conflicts == 1
    ot = env.ot_by_title("TT wins")
    assert ot["description"] == "Q3 numbers"
    env.assert_quiet()


def test_complete_in_opentodo_completes_in_ticktick(env: Env) -> None:
    _, b = seed(env)
    env.ot_by_title("Report")["completed_at"] = 123
    res = env.run()
    assert res.tt.completed == 1
    assert env.tt.tasks[b]["status"] == 2
    env.assert_quiet()  # gone from TickTick's undone list, but known completed: no lookups


def test_complete_in_ticktick_completes_in_opentodo(env: Env) -> None:
    _, b = seed(env)
    env.tt.tasks[b]["status"] = 2
    res = env.run()
    assert res.ot.completed == 1
    assert env.ot_by_title("Report")["completed_at"] is not None
    env.assert_quiet()


def test_wont_do_counts_as_completed(env: Env) -> None:
    _, b = seed(env)
    env.tt.tasks[b]["status"] = -1
    env.run()
    assert env.ot_by_title("Report")["completed_at"] is not None


def test_completed_then_deleted_in_ticktick_then_edited_in_opentodo(env: Env) -> None:
    """It failed every pass: a completed pair isn't fetched again, so the deletion went
    unseen, and writing the OpenTodo edit back found no TickTick task."""
    a, _ = seed(env)
    env.tt.tasks[a]["status"] = 2
    env.run()  # completed in OpenTodo too
    del env.tt.tasks[a]  # then deleted in TickTick
    env.ot_by_title("Inbox thing")["title"] = "Inbox thing, edited"
    res = env.run()
    assert res.ok and res.ot.deleted == 1, res.summary()  # deleted in TickTick wins
    assert env.ot_by_title("Inbox thing, edited")["deleted_at"] is not None
    env.assert_quiet()


def test_reopen_in_opentodo_reopens_in_ticktick(env: Env) -> None:
    _, b = seed(env)
    env.tt.tasks[b]["status"] = 2
    env.run()
    env.ot_by_title("Report")["completed_at"] = None
    res = env.run()
    assert res.tt.updated == 1
    assert env.tt.tasks[b]["status"] == 0
    env.assert_quiet()


def test_recurring_completion_in_opentodo_advances(env: Env) -> None:
    _, b = seed(env)
    env.tt.tasks[b]["repeatFlag"] = "RRULE:FREQ=DAILY"
    env.run()  # the rule goes to OpenTodo
    assert env.ot_by_title("Report")["recurrence"] == "every day"
    # A completion without OpenTodo's own repeat logic (as a plain REST client would do it).
    env.ot_by_title("Report")["completed_at"] = 1
    env.run()  # completes in TickTick, which advances the task by a day
    env.run()  # the advanced instance flows back and reopens the OpenTodo task
    ot = env.ot_by_title("Report")
    assert ot["completed_at"] is None
    assert ot["due"] == {"date": "2026-11-26", "time": "09:00", "tz": NY}
    env.assert_quiet()


def test_delete_in_ticktick_deletes_in_opentodo(env: Env) -> None:
    _, b = seed(env)
    del env.tt.tasks[b]
    res = env.run()
    assert res.ot.deleted == 1
    assert env.ot_by_title("Report")["deleted_at"] is not None
    assert env.state.pair_count() == 1
    env.assert_quiet()


def test_delete_in_opentodo_deletes_in_ticktick(env: Env) -> None:
    _, b = seed(env)
    env.ot_by_title("Report")["deleted_at"] = 1
    res = env.run()
    assert res.tt.deleted == 1 and b not in env.tt.tasks
    env.assert_quiet()


def test_delete_in_opentodo_but_changed_in_ticktick_recreates(env: Env) -> None:
    _, b = seed(env)
    env.ot_by_title("Report")["deleted_at"] = 1
    env.tt.tasks[b]["title"] = "Report (edited)"
    res = env.run()
    assert res.ot.created == 1 and b in env.tt.tasks
    assert env.ot_by_title("Report (edited)")["deleted_at"] is None
    env.assert_quiet()


def test_move_between_projects_in_opentodo(env: Env) -> None:
    a, _ = seed(env)
    otp = next(iter(env.ot.projects_))
    env.ot_by_title("Inbox thing")["project_id"] = otp
    env.run()
    assert env.tt.projects_[env.tt.tasks[a]["projectId"]]["name"] == "Work"
    assert ("move", a) in env.tt.writes
    env.assert_quiet()


def _moved_in_ticktick(env: Env) -> str:
    """'Report' moved from Work to a new list Home in the TickTick app, and synced."""
    _, b = seed(env)
    home = env.tt.add_project("Home")
    env.tt.tasks[b]["projectId"] = home
    env.run()
    assert next(p for p in env.state.pairs() if p.tt_id == b).tt_project == home
    return b


def test_moved_in_ticktick_then_completed_completes_in_opentodo(env: Env) -> None:
    """The completed task was looked up under its old list: a 404 read as "deleted"."""
    b = _moved_in_ticktick(env)
    env.tt.tasks[b]["status"] = 2
    res = env.run()
    assert res.ot.completed == 1 and not res.ot.deleted, res.summary()
    report = env.ot_by_title("Report")
    assert report["completed_at"] is not None and report["deleted_at"] is None
    env.assert_quiet()


def test_moved_in_ticktick_then_deleted_in_opentodo_stays_deleted(env: Env) -> None:
    """The DELETE went to the old list: its 404 was ignored and the task came back."""
    b = _moved_in_ticktick(env)
    env.ot.delete_task(env.ot_by_title("Report")["id"])
    res = env.run()
    assert res.tt.deleted == 1, res.summary()
    assert b not in env.tt.tasks
    env.assert_quiet()
    assert all(t["title"] != "Report" for t in env.ot.live())


def test_bulk_guard_blocks_then_allow_bulk(env: Env) -> None:
    a, b = seed(env)
    env.max_destructive = 1
    del env.tt.tasks[a], env.tt.tasks[b]
    ot_writes = len(env.ot.writes)
    res = env.run()
    assert res.blocked and not res.ok
    assert len(env.ot.writes) == ot_writes
    assert env.state.get_meta_json("blocked")["count"] == 2
    res = env.run(allow_bulk=True)
    assert res.ot.deleted == 2 and res.ok
    assert env.state.get_meta("blocked") is None


def test_first_sync_refuses_when_opentodo_has_open_tasks(env: Env) -> None:
    """Nothing at all is written, lists and projects included (they used to be)."""
    env.tt.add_task("tt", env.tt.add_project("Work"))
    env.ot.add_task("leftover from import", env.ot.add_project("Old"))
    res = env.run()
    assert res.refused and env.ot.writes == [] and env.tt.writes == []


def test_unwritable_state_writes_nothing_to_either_app(env: Env) -> None:
    """A create whose pair can't be recorded is made again by every later pass, and its copy
    copied back: before the write check, the duplicates doubled each pass (disk full)."""
    seed(env)
    env.tt.add_task("New in TickTick")
    env.ot.add_task("New in OpenTodo")
    writes = (len(env.tt.writes), len(env.ot.writes))
    deny_writes(env.state)
    for _ in range(3):
        with pytest.raises(sqlite3.DatabaseError):
            env.run()
    assert (len(env.tt.writes), len(env.ot.writes)) == writes

    env.state.db.set_authorizer(None)  # space freed
    res = env.run()
    assert (res.tt.created, res.ot.created) == (1, 1), res.summary()
    env.assert_quiet()


def test_state_error_mid_pass_stops_it(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The disk fills after the write check: the pass stops at the first pair it can't record."""
    seed(env)
    env.tt.add_task("One")
    env.tt.add_task("Two")

    def disk_full(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(State, "put_pair", disk_full)
    with pytest.raises(sqlite3.OperationalError):
        env.run()
    assert len([t for t in env.ot.live() if t["title"] in ("One", "Two")]) == 1


def test_closed_list_is_out_of_scope(env: Env) -> None:
    old = env.tt.add_project("Archive", closed=True)
    env.tt.add_task("old stuff", old)
    env.run()
    assert env.ot.projects_ == {} and env.ot.tasks_ == {}


def test_subtask_of_unknown_parent_ignored(env: Env) -> None:
    env.tt.add_task("child", parentId="someone")
    env.run()
    assert env.ot.tasks_ == {}


def test_match_existing_then_ticktick_wins(env: Env) -> None:
    work = env.tt.add_project("Work")
    tt_id = env.tt.add_task("Report", work, content="TT notes")
    otp = env.ot.add_project("work")
    env.ot.add_task("Report", otp, description="imported notes")
    env.ot.add_task("Only in OT", otp)
    syncer = Syncer(env.tt, env.ot, env.state)
    m = syncer.match_existing(dry_run=False)
    assert len(m.matches) == 1 and len(m.ot_unmatched) == 1
    res = env.run()
    assert res.ok and res.ot.updated == 1 and res.tt.created == 1
    assert env.ot_by_title("Report")["description"] == "TT notes"
    assert tt_id in env.tt.tasks
    env.assert_quiet()


def test_inbox_endpoint_404_degrades(env: Env) -> None:
    from ticktick_opentodo_sync.http import ApiError

    work = env.tt.add_project("Work")
    env.tt.add_task("in work", work)
    original = env.tt.project_data

    def project_data(pid: str) -> dict[str, object]:
        if pid == "inbox":
            raise ApiError("TickTick", "GET", "/open/v1/project/inbox/data", 404, "")
        return original(pid)

    env.tt.project_data = project_data  # type: ignore[method-assign,assignment]
    res = env.run()
    assert res.ok and res.ot.created == 1


# --- recurrence (phase 2) --------------------------------------------------------------------


def _weekly(env: Env, **fields: Any) -> str:
    """A TickTick task due Monday 2026-11-23 that repeats every Monday."""
    tid = env.tt.add_task(
        "Review",
        dueDate="2026-11-23T05:00:00+0000",
        isAllDay=True,
        timeZone=NY,
        repeatFlag="RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=1;BYDAY=MO",
        repeatFrom="0",
    )
    env.tt.tasks[tid] |= fields
    assert env.run().ok
    return tid


def test_rule_goes_to_opentodo_and_back(env: Env) -> None:
    tid = _weekly(env)
    ot = env.ot_by_title("Review")
    assert (ot["recurrence"], ot["due"]) == ("every monday", {"date": "2026-11-23"})
    env.assert_quiet()

    ot["recurrence"] = "every 2 weeks after completion"
    assert env.run().tt.updated == 1
    assert env.tt.tasks[tid]["repeatFlag"] == "RRULE:FREQ=WEEKLY;INTERVAL=2"
    assert env.tt.tasks[tid]["repeatFrom"] == "1"
    env.assert_quiet()

    ot["recurrence"] = None
    env.run()
    assert env.tt.tasks[tid]["repeatFlag"] == ""
    env.assert_quiet()


def test_untranslatable_rule_stays_in_ticktick(env: Env) -> None:
    tid = env.tt.add_task(
        "Water plants",
        dueDate="2026-12-06T05:00:00+0000",
        isAllDay=True,
        timeZone=NY,
        repeatFlag="RRULE:FREQ=MONTHLY;BYMONTHDAY=6,10",
    )
    env.run()
    assert env.ot_by_title("Water plants")["recurrence"] is None
    env.ot_by_title("Water plants")["title"] = "Water plants!"
    env.run()
    assert env.tt.tasks[tid]["title"] == "Water plants!"
    assert env.tt.tasks[tid]["repeatFlag"] == "RRULE:FREQ=MONTHLY;BYMONTHDAY=6,10"
    env.assert_quiet()


def test_completing_repeating_task_in_opentodo_completes_once_in_ticktick(env: Env) -> None:
    tid = _weekly(env)
    ot = env.ot_by_title("Review")
    # OpenTodo's completion: the task moves on, the finished date becomes an occurrence record.
    ot["due"] = {"date": "2026-11-30"}
    env.ot.add_task(
        "Review", None, series_id=ot["id"], occurrence_date="2026-11-23", completed_at=1
    )
    res = env.run()
    assert res.tt.completed == 1 and res.ot.updated == 0, res.summary()
    assert ("complete", tid) in env.tt.writes
    # The fake TickTick advances by a day; TickTick's date wins on the next pass.
    env.run()
    assert ot["due"] == {"date": "2026-11-24"} and ot["completed_at"] is None
    assert sum(w == ("complete", tid) for w in env.tt.writes) == 1
    env.assert_quiet()


def test_skipping_in_opentodo_moves_the_ticktick_date(env: Env) -> None:
    tid = _weekly(env)
    env.ot_by_title("Review")["due"] = {"date": "2026-11-30"}  # skip: no occurrence record
    res = env.run()
    assert res.tt.updated == 1 and res.tt.completed == 0, res.summary()
    assert env.tt.tasks[tid]["dueDate"].startswith("2026-11-30")
    env.assert_quiet()


def test_tasks_without_rules_keep_their_pre_phase_2_hash() -> None:
    from ticktick_opentodo_sync.models import Task

    assert "recurrence" not in Task("x").to_json()


# --- subtasks and checklist items ------------------------------------------------------------


def _checklist(env: Env, **fields: Any) -> str:
    tid = env.tt.add_task(
        "Pack",
        kind="CHECKLIST",
        items=[
            {"id": "i1", "title": "Socks", "status": 0, "sortOrder": 1},
            {"id": "i2", "title": "Shirts", "status": 1, "sortOrder": 2},
            {"id": "i3", "title": "Charger", "status": 0, "sortOrder": 3},
        ],
        **fields,
    )
    env.run()  # the task
    env.run()  # its items, once the task is paired
    return tid


def subtasks(env: Env, parent_title: str) -> dict[str, dict[str, Any]]:
    parent = env.ot_by_title(parent_title)["id"]
    return {
        str(t["title"]): t
        for t in env.ot.tasks_.values()
        if t["parent_id"] == parent and t["deleted_at"] is None
    }


def tt_items(env: Env, tid: str) -> dict[str, dict[str, Any]]:
    return {str(i["title"]): i for i in env.tt.tasks[tid]["items"]}


def test_whole_task_write_keeps_a_tick_made_during_the_pass(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write was built on the copy read at the start of the pass, checklist included: a
    tick made in TickTick meanwhile was undone."""
    tid = _checklist(env)
    env.ot_by_title("Pack")["due"] = {"date": "2026-12-01"}  # edited in OpenTodo
    check_writable = State.check_writable

    def ticked_meanwhile(state: State) -> None:
        # Runs once the pass has read both apps, just before it writes.
        tt_items(env, tid)["Socks"]["status"] = 1  # ticked in TickTick meanwhile
        check_writable(state)

    monkeypatch.setattr(State, "check_writable", ticked_meanwhile)
    res = env.run()
    assert res.ok and not res.tt.updated, res.summary()  # left for the next pass
    assert tt_items(env, tid)["Socks"]["status"] == 1
    monkeypatch.undo()
    res = env.run()
    assert res.tt.updated == 1, res.summary()
    assert env.tt.tasks[tid]["dueDate"] and tt_items(env, tid)["Socks"]["status"] == 1
    assert subtasks(env, "Pack")["Socks"]["completed_at"] is not None
    env.assert_quiet()


def test_checklist_items_become_subtasks(env: Env) -> None:
    _checklist(env)
    subs = subtasks(env, "Pack")
    assert set(subs) == {"Socks", "Shirts", "Charger"}
    assert subs["Shirts"]["completed_at"] is not None and subs["Socks"]["completed_at"] is None
    assert [subs[t]["position"] for t in ("Socks", "Shirts", "Charger")] == [1, 2, 3]
    env.assert_quiet()


def test_checklist_changes_both_ways(env: Env) -> None:
    tid = _checklist(env)
    subtasks(env, "Pack")["Socks"]["completed_at"] = 1  # ticked in OpenTodo
    res = env.run()
    assert res.tt_items.updated == 1, res.summary()
    assert tt_items(env, tid)["Socks"]["status"] == 1
    env.assert_quiet()

    tt_items(env, tid)["Charger"]["title"] = "USB-C charger"  # renamed in TickTick
    env.run()
    assert "USB-C charger" in subtasks(env, "Pack")
    env.assert_quiet()

    parent_ot = env.ot_by_title("Pack")
    env.ot.add_task("Toothbrush", parent_ot["project_id"], parent_id=parent_ot["id"])
    res = env.run()
    assert res.tt_items.created == 1, res.summary()
    assert tt_items(env, tid)["Toothbrush"]["status"] == 0
    env.assert_quiet()

    env.tt.tasks[tid]["items"] = [i for i in env.tt.tasks[tid]["items"] if i["title"] != "Shirts"]
    env.run()
    assert "Shirts" not in subtasks(env, "Pack")
    env.assert_quiet()

    subtasks(env, "Pack")["Toothbrush"]["deleted_at"] = 1
    res = env.run()
    assert res.tt_items.deleted == 1, res.summary()
    assert "Toothbrush" not in tt_items(env, tid)
    env.assert_quiet()


def test_checklist_conflict_ticktick_wins_even_unticking(env: Env) -> None:
    tid = _checklist(env)
    tt_items(env, tid)["Shirts"]["status"] = 0
    subtasks(env, "Pack")["Shirts"]["title"] = "Shirts!"
    res = env.run()
    assert res.conflicts == 1, res.summary()
    shirts = subtasks(env, "Pack")["Shirts"]
    assert shirts["completed_at"] is None
    env.assert_quiet()


def test_new_subtask_under_plain_task_becomes_ticktick_subtask(env: Env) -> None:
    env.tt.add_task("Plan trip")
    env.run()
    parent_ot = env.ot_by_title("Plan trip")
    env.ot.add_task("Book hotel", parent_ot["project_id"], parent_id=parent_ot["id"])
    res = env.run()
    assert res.tt.created == 1, res.summary()
    child = next(t for t in env.tt.tasks.values() if t["title"] == "Book hotel")
    parent_tt = next(t for t in env.tt.tasks.values() if t["title"] == "Plan trip")
    assert child["parentId"] == parent_tt["id"]
    env.assert_quiet()


def test_ticktick_subtask_goes_to_opentodo(env: Env) -> None:
    parent = env.tt.add_task("Plan trip")
    env.tt.add_task("Book hotel", parentId=parent)
    res = env.run()
    assert res.ot.created == 2, res.summary()
    assert env.ot_by_title("Book hotel")["parent_id"] == env.ot_by_title("Plan trip")["id"]
    env.assert_quiet()


def test_repeating_checklist_completed_in_opentodo(env: Env) -> None:
    tid = _checklist(
        env,
        dueDate="2026-11-23T05:00:00+0000",
        isAllDay=True,
        timeZone=NY,
        repeatFlag="RRULE:FREQ=WEEKLY;BYDAY=MO",
        repeatFrom="0",
    )
    parent = env.ot_by_title("Pack")
    # OpenTodo completes the occurrence: subtasks done, task moved on, occurrence recorded.
    for sub in subtasks(env, "Pack").values():
        sub["completed_at"] = 1
    parent["due"] = {"date": "2026-11-30"}
    env.ot.add_task(
        "Pack", None, series_id=parent["id"], occurrence_date="2026-11-23", completed_at=1
    )
    res = env.run()
    assert res.tt.completed == 1 and res.tt_items.updated == 0, res.summary()
    # TickTick moves on and resets its checklist for the next occurrence.
    for item in env.tt.tasks[tid]["items"]:
        item["status"] = 0
    res = env.run()
    assert res.conflicts == 0 and res.ot_items.updated == 3, res.summary()
    assert all(s["completed_at"] is None for s in subtasks(env, "Pack").values())
    assert all(i["status"] == 0 for i in env.tt.tasks[tid]["items"])
    env.assert_quiet()


# --- reminders --------------------------------------------------------------------------------

TIMED: dict[str, Any] = {"dueDate": "2026-11-25T14:00:00+0000", "isAllDay": False, "timeZone": NY}
ALL_DAY: dict[str, Any] = {"dueDate": "2026-11-25T05:00:00+0000", "isAllDay": True, "timeZone": NY}


def test_ticktick_reminders_are_seeded(env: Env) -> None:
    env.tt.add_task("Timed", reminders=["TRIGGER:PT0S"], **TIMED)
    env.tt.add_task("Morning", reminders=["TRIGGER:P0DT7H0M0S"], **ALL_DAY)
    assert env.run().ot.created == 2
    assert env.ot_by_title("Timed")["reminders"] == [0]
    assert env.ot_by_title("Morning")["reminders"] == [-120]
    env.assert_quiet()


def test_reminder_added_in_ticktick_to_an_existing_pair(env: Env) -> None:
    tid = env.tt.add_task("Call", **TIMED)
    env.run()
    env.tt.tasks[tid]["reminders"] = ["TRIGGER:-PT30M"]
    res = env.run()
    assert res.ot.updated == 1 and not res.tt.updated, res.summary()
    assert env.ot_by_title("Call")["reminders"] == [-30]
    env.assert_quiet()


def test_reminders_changed_in_opentodo_go_to_ticktick(env: Env) -> None:
    tid = env.tt.add_task("Call", reminders=["TRIGGER:-PT0S"], **TIMED)
    env.run()
    env.ot_by_title("Call")["reminders"] = [-30, 0]
    res = env.run()
    assert res.tt.updated == 1 and not res.ot.updated, res.summary()
    assert env.tt.tasks[tid]["reminders"] == ["TRIGGER:-PT30M", "TRIGGER:-PT0S"]
    env.assert_quiet()

    env.ot_by_title("Call")["reminders"] = []
    assert env.run().tt.updated == 1
    assert env.tt.tasks[tid]["reminders"] == []
    env.assert_quiet()


def test_reminder_conflict_ticktick_wins(env: Env) -> None:
    tid = env.tt.add_task("Call", reminders=["TRIGGER:PT0S"], **TIMED)
    env.run()
    env.tt.tasks[tid]["reminders"] = ["TRIGGER:-PT1H"]
    env.ot_by_title("Call")["reminders"] = [-15]
    res = env.run()
    assert res.conflicts == 1 and res.ot.updated == 1, res.summary()
    assert env.ot_by_title("Call")["reminders"] == [-60]
    assert env.tt.tasks[tid]["reminders"] == ["TRIGGER:-PT1H"]
    env.assert_quiet()


# --- repeating tasks completed in TickTick ----------------------------------------------------


def test_completing_repeating_task_in_ticktick_completes_the_opentodo_occurrence(
    env: Env,
) -> None:
    tid = _weekly(env)
    env.tt.complete_task("", tid)  # like the app: a completed copy, the task moves on a day
    env.tt.writes.clear()
    res = env.run()
    assert res.ot.updated == 1 and res.ok, res.summary()
    ot = env.ot_by_title("Review")
    occ = [w for w in env.ot.writes if w[0] == "complete_occurrence"]
    assert occ == [("complete_occurrence", (ot["id"], "2026-11-23", 1795402800000))]
    records = [t for t in env.ot.tasks_.values() if t.get("series_id") == ot["id"]]
    assert [(r["occurrence_date"], r["completed_at"]) for r in records] == [
        ("2026-11-23", 1795402800000)
    ]
    assert ot["due"]["date"] == "2026-11-24" and ot["completed_at"] is None
    assert not env.tt.writes  # nothing goes back to TickTick
    env.assert_quiet()


def test_moving_repeating_task_in_ticktick_only_moves_opentodo(env: Env) -> None:
    tid = _weekly(env)
    env.tt.tasks[tid]["dueDate"] = env.tt.tasks[tid]["startDate"] = "2026-11-30T05:00:00+0000"
    res = env.run()
    assert res.ot.updated == 1, res.summary()
    assert not [w for w in env.ot.writes if w[0] == "complete_occurrence"]
    assert env.ot_by_title("Review")["due"]["date"] == "2026-11-30"
    env.assert_quiet()


def test_after_completion_rule_completed_in_ticktick_only_moves(env: Env) -> None:
    tid = _weekly(env, repeatFrom="1")
    env.tt.complete_task("", tid)
    env.run()
    assert not [w for w in env.ot.writes if w[0] == "complete_occurrence"]
    assert env.ot_by_title("Review")["due"]["date"] == "2026-11-24"
    env.assert_quiet()


def test_opentodo_refusing_the_occurrence_falls_back_to_a_move(env: Env) -> None:
    tid = _weekly(env)
    env.ot.refuse_occurrences = True
    env.tt.complete_task("", tid)
    res = env.run()
    assert res.ok and res.ot.updated == 1, res.summary()
    assert env.ot_by_title("Review")["due"]["date"] == "2026-11-24"
    env.assert_quiet()
