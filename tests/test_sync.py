"""End-to-end passes against in-memory fakes of both APIs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import FakeOpenTodo, FakeTickTick

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

    def ot_by_title(self, title: str) -> dict[str, object]:
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
    assert [env.ot.label_name(x) for x in report["labels"]] == ["office"]  # type: ignore[attr-defined]
    assert env.state.pair_count() == 2
    assert env.state.get_meta("tt_inbox_id") == "inbox100"
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
    names = sorted(env.ot.label_name(x) for x in env.ot_by_title("Report")["labels"])  # type: ignore[attr-defined]
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
    env.tt.add_task("tt")
    env.ot.add_task("leftover from import")
    res = env.run()
    assert res.refused and env.ot.writes == [] and env.tt.writes == []


def test_closed_list_is_out_of_scope(env: Env) -> None:
    old = env.tt.add_project("Archive", closed=True)
    env.tt.add_task("old stuff", old)
    env.run()
    assert env.ot.projects_ == {} and env.ot.tasks_ == {}


def test_subtasks_ignored(env: Env) -> None:
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


def _weekly(env: Env) -> str:
    """A TickTick task due Monday 2026-11-23 that repeats every Monday."""
    tid = env.tt.add_task(
        "Review",
        dueDate="2026-11-23T05:00:00+0000",
        isAllDay=True,
        timeZone=NY,
        repeatFlag="RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=1;BYDAY=MO",
        repeatFrom="0",
    )
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
