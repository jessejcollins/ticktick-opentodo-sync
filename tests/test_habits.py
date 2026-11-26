"""Habits: the pure mapping and planning (habits.py) and whole passes against the fakes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeOpenTodo, FakeTickTick, deny_writes

from ticktick_opentodo_sync.habit_sync import META_PROJECT
from ticktick_opentodo_sync.habits import (
    DKind,
    HabitDef,
    HabitPair,
    HKind,
    Unsupported,
    habit_from_ot,
    habit_from_tt,
    habit_to_ot,
    habit_to_tt,
    match_habits,
    ot_done_days,
    plan_days,
    plan_habits,
    tt_done_days,
    window,
)
from ticktick_opentodo_sync.opentodo import PLACEHOLDER
from ticktick_opentodo_sync.state import State, habit_refused
from ticktick_opentodo_sync.sync import PassResult, Syncer

DAILY = "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=SU,MO,TU,WE,TH,FR,SA"
EVERY_DAY = "every monday, tuesday, wednesday, thursday, friday, saturday, sunday"
SECTIONS = {"s-m": "_morning", "s-a": "_afternoon", "s-n": "_night"}
SECTION_IDS = {v: k for k, v in SECTIONS.items()}


# --- mapping ----------------------------------------------------------------------------------


def test_habit_from_tt() -> None:
    raw = {
        "name": " Read ",
        "type": "Boolean",
        "goal": 1.0,
        "repeatRule": DAILY,
        "sectionId": "s-n",
        "reminders": ["21:00", "07:30"],
        "targetDays": 0,
    }
    assert habit_from_tt(raw, SECTIONS) == HabitDef(
        "Read", EVERY_DAY, "evening", frozenset({720, -90})
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "Real", "goal": 8.0},
        {"goal": 3.0},
        {"repeatRule": "RRULE:FREQ=WEEKLY;TT_TIMES=3"},
        {"targetDays": 30},
        {"reminders": ["soon"]},
        {"repeatRule": "RRULE:FREQ=MONTHLY;BYMONTHDAY=6,10"},
    ],
)
def test_habit_from_tt_unsupported(fields: dict[str, Any]) -> None:
    raw = {"name": "x", "type": "Boolean", "goal": 1.0, "repeatRule": DAILY} | fields
    with pytest.raises(Unsupported):
        habit_from_tt(raw, SECTIONS)


def test_habit_to_tt_keeps_spelling_and_creates_yes_no_habits() -> None:
    h = HabitDef("Read", EVERY_DAY, "afternoon", frozenset({240}))
    raw = {"repeatRule": DAILY, "sectionId": "s-m"}
    assert habit_to_tt(h, raw, SECTION_IDS) == {
        "name": "Read",
        "sectionId": "s-a",
        "reminders": ["13:00"],
    }
    new = habit_to_tt(HabitDef("Walk", "every day", None, frozenset({-1440})), None, SECTION_IDS)
    assert new["repeatRule"] == "RRULE:FREQ=DAILY"
    assert new["reminders"] == []  # a day early can't be a habit reminder time
    assert (new["type"], new["goal"], new["status"]) == ("Boolean", 1.0, 0)


def test_habit_ot_round_trip() -> None:
    for part in ("morning", "afternoon", "evening", None):
        h = HabitDef("Read", EVERY_DAY, part, frozenset({0, 240}))
        body = habit_to_ot(h)
        assert body["habit"] is True
        assert habit_from_ot(body | {"id": "o"}) == h


def test_done_days() -> None:
    docs = [
        {
            "habitId": "h",
            "checkins": [
                {"stamp": 20261120, "status": 2, "opTime": "2026-11-20T10:00:00+0000"},
                {"stamp": 20261121, "status": 2, "opTime": "2026-11-21T10:00:00+0000"},
                {
                    "stamp": 20261121,
                    "status": 0,
                    "opTime": "2026-11-21T11:00:00+0000",
                },  # undone later
                {"stamp": 20261122, "status": 1, "opTime": "x"},  # "not completed"
            ],
        }
    ]
    assert tt_done_days(docs) == {("h", "2026-11-20")}
    records = [
        {"series_id": "o", "occurrence_date": "2026-11-20", "completed_at": 1, "deleted_at": None},
        {
            "series_id": "o",
            "occurrence_date": "2026-11-21",
            "completed_at": 1,
            "skipped": True,
            "deleted_at": None,
        },
        {"series_id": "o", "occurrence_date": "2026-11-22", "completed_at": 1, "deleted_at": 5},
        {
            "series_id": "o",
            "occurrence_date": "2026-11-23",
            "completed_at": 1,
            "tiny": True,
            "deleted_at": None,
        },
    ]
    assert ot_done_days(records) == {("o", "2026-11-20"), ("o", "2026-11-23")}


# --- planning ---------------------------------------------------------------------------------

H = HabitDef("Read", EVERY_DAY, "evening")


def hp(tt: HabitDef = H, ot: HabitDef = H) -> HabitPair:
    return HabitPair("t", "o", tt.content_hash(), ot.content_hash(), H)


def test_plan_habits() -> None:
    renamed = HabitDef("Read more", EVERY_DAY, "evening")
    assert plan_habits([hp()], {"t": H}, {"o": H}) == []
    (a,) = plan_habits([hp()], {"t": renamed}, {"o": H})
    assert (a.kind, a.habit, a.conflict) == (HKind.UPDATE_OT, renamed, False)
    (a,) = plan_habits([hp()], {"t": H}, {"o": renamed})
    assert (a.kind, a.habit) == (HKind.UPDATE_TT, renamed)
    other = HabitDef("Reading", EVERY_DAY, "evening")
    (a,) = plan_habits([hp()], {"t": renamed}, {"o": other})
    assert (a.kind, a.habit, a.conflict) == (HKind.UPDATE_OT, renamed, True)
    (a,) = plan_habits([hp()], {"t": renamed}, {"o": renamed})
    assert a.kind is HKind.REHASH
    assert plan_habits([hp()], {}, {"o": renamed}) == []  # deletions aren't synced
    kinds = [a.kind for a in plan_habits([], {"t": H}, {"o": H})]
    assert kinds == [HKind.CREATE_OT, HKind.CREATE_TT]


def test_match_habits() -> None:
    walk, run = HabitDef("Walk", EVERY_DAY), HabitDef("Run", EVERY_DAY)
    tt = {"t1": H, "t2": walk, "t3": run, "t4": run, "t5": HabitDef("Only in TickTick")}
    ot = {"o1": HabitDef(" read "), "o2": HabitDef("WALK"), "o3": run, "o4": HabitDef("Mine")}
    pairs = [HabitPair("t1", "o9", "", "")]  # t1 is paired with another habit already
    matches, ambiguous = match_habits(pairs, tt, ot)
    assert matches == [("t2", "o2")]  # by name, ignoring case and spaces
    assert ambiguous == {"run"}  # twice in TickTick: neither side's copy is made


def test_plan_days() -> None:
    days = ["d1", "d2", "d3", "d4", "d5"]
    tt = {("t", "d1"), ("t", "d2"), ("t", "d4")}
    ot = {("o", "d1"), ("o", "d3"), ("o", "d5")}
    base = {("t", "d1"), ("t", "d3"), ("t", "d4")}
    rows = {a.day: a.kind for a in plan_days([("t", "o")], days, tt, ot, base)}
    assert rows == {
        "d2": DKind.CHECK_OT,  # TickTick checked it
        "d3": DKind.CLEAR_OT,  # TickTick un-checked it
        "d4": DKind.CLEAR_TT,  # OpenTodo un-checked it
        "d5": DKind.CHECK_TT,  # OpenTodo checked it
    }
    # A check-in OpenTodo refused isn't offered again; nothing else changes.
    refused = {("t", "d2"), ("t", "d3")}
    rows = {a.day: a.kind for a in plan_days([("t", "o")], days, tt, ot, base, refused=refused)}
    assert rows == {"d3": DKind.CLEAR_OT, "d4": DKind.CLEAR_TT, "d5": DKind.CHECK_TT}


def test_window() -> None:
    from datetime import date

    assert window(date(2026, 11, 24), 3) == ["2026-11-22", "2026-11-23", "2026-11-24"]


# --- whole passes -----------------------------------------------------------------------------

NOW = int(datetime(2026, 11, 24, 15, 0, tzinfo=UTC).timestamp() * 1000)  # 10:00 in New York


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tt = FakeTickTick()
        self.ot = FakeOpenTodo()
        self.ot.today = "2026-11-24"
        self.state = State(tmp / "state.db")

    def run(self, *, dry_run: bool = False) -> PassResult:
        res = Syncer(self.tt, self.ot, self.state, now_ms=lambda: NOW).run(dry_run=dry_run)
        assert res.ok, res.summary()
        return res

    def quiet(self) -> None:
        tt_w, ot_w = len(self.tt.writes), len(self.ot.writes)
        res = self.run()
        assert not res.changed, res.summary()
        assert (len(self.tt.writes), len(self.ot.writes)) == (tt_w, ot_w)

    def ot_habit(self, title: str) -> dict[str, Any]:
        return next(t for t in self.ot.tasks_.values() if t.get("habit") and t["title"] == title)

    def ot_days(self, habit: dict[str, Any]) -> list[str]:
        return sorted(
            t["occurrence_date"]
            for t in self.ot.tasks_.values()
            if t.get("series_id") == habit["id"] and t["deleted_at"] is None
        )

    def tt_days(self, hid: str) -> list[int]:
        return sorted(s for (h, s), status in self.tt.checkins.items() if h == hid and status == 2)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.state.close()


def test_ticktick_habits_are_created_with_30_days_of_check_ins(env: Env) -> None:
    hid = env.tt.add_habit("Read", sectionId="s-n", reminders=["21:00"])
    for stamp in (20261020, 20261026, 20261120, 20261121, 20261124):  # 1st is outside the window
        env.tt.checkins[(hid, stamp)] = 2
    env.tt.checkins[(hid, 20261122)] = 1  # "not completed"
    res = env.run()
    assert res.habits_ot.created == 1 and res.habits_ot.completed == 4, res.summary()

    habit = env.ot_habit("Read")
    assert (habit["recurrence"], habit["evening"], habit["reminders"]) == (EVERY_DAY, True, [720])
    assert habit["project_id"] == env.state.get_meta(META_PROJECT)
    assert env.ot.projects_[habit["project_id"]]["name"] == "Habits"
    assert env.ot_days(habit) == ["2026-10-26", "2026-11-20", "2026-11-21", "2026-11-24"]
    assert habit["due"] == {"date": "2026-11-25"}  # today is done: on to tomorrow
    assert not [p for p in env.tt.projects_.values() if p["name"] == "Habits"]
    env.quiet()


def test_check_ins_sync_both_ways(env: Env) -> None:
    hid = env.tt.add_habit("Read")
    env.tt.checkins[(hid, 20261123)] = 2
    env.run()
    habit = env.ot_habit("Read")

    env.ot.log_habit(habit["id"], "2026-11-24", "done", None)  # checked in OpenTodo today
    env.ot.writes.clear()
    res = env.run()
    assert res.habits_tt.completed == 1, res.summary()
    assert env.tt_days(hid) == [20261123, 20261124]
    env.quiet()

    env.tt.checkins[(hid, 20261123)] = 0  # un-checked in TickTick
    res = env.run()
    assert res.habits_ot.deleted == 1, res.summary()
    assert env.ot_days(habit) == ["2026-11-24"]
    env.quiet()

    env.ot.log_habit(habit["id"], "2026-11-24", "clear", None)  # un-checked in OpenTodo
    res = env.run()
    assert res.habits_tt.deleted == 1, res.summary()
    assert env.tt_days(hid) == []
    assert env.tt.checkins[(hid, 20261124)] == 0
    env.quiet()


def test_missed_days_then_a_check_in_moves_the_opentodo_habit(env: Env) -> None:
    hid = env.tt.add_habit("Walk")
    env.tt.checkins[(hid, 20261110)] = 2
    env.run()
    habit = env.ot_habit("Walk")
    # Logging 11-10 jumps OpenTodo past today; the sync puts it back on today, still open.
    assert habit["due"] == {"date": "2026-11-24"}
    env.tt.checkins[(hid, 20261123)] = 2
    env.run()
    assert env.ot_days(habit) == ["2026-11-10", "2026-11-23"]
    assert habit["due"] == {"date": "2026-11-24"}
    env.quiet()


def test_day_opentodo_refuses_stays_in_ticktick(env: Env) -> None:
    """OpenTodo takes no day before a habit's first one: a TickTick back-fill stays there.

    It used to be recorded as synced anyway, so the next pass read OpenTodo's gap as an
    un-check and cleared the day in TickTick.
    """
    hid = env.tt.add_habit("Read")
    env.tt.checkins[(hid, 20261120)] = 2
    env.run()  # the OpenTodo habit starts on 11-20
    habit = env.ot_habit("Read")
    env.tt.checkins[(hid, 20261115)] = 2  # back-filled in TickTick
    res = env.run()  # OpenTodo refuses 11-15
    assert not res.habits_ot.completed and not res.habits_tt.deleted, res.summary()
    assert habit_refused(env.state) == {(hid, "2026-11-15")}
    env.quiet()  # not offered again, and not cleared in TickTick
    assert env.tt_days(hid) == [20261115, 20261120]
    assert env.ot_days(habit) == ["2026-11-20"]

    env.tt.checkins[(hid, 20261115)] = 0  # un-checked in TickTick: nothing left to remember
    env.quiet()
    assert habit_refused(env.state) == set()


def test_opentodo_habit_is_created_in_ticktick(env: Env) -> None:
    oid = env.ot.add_task(
        "Stretch",
        None,
        habit=True,
        recurrence="every day",
        due={"date": "2026-11-23"},
        day_part="afternoon",
        evening=False,
        reminders=[240],
    )
    env.ot.log_habit(oid, "2026-11-23", "done", None)
    res = env.run()
    assert res.habits_tt.created == 1 and res.habits_tt.completed == 1, res.summary()
    (h,) = env.tt.habits_.values()
    assert (h["name"], h["repeatRule"], h["sectionId"], h["reminders"]) == (
        "Stretch",
        "RRULE:FREQ=DAILY",
        "s-a",
        ["13:00"],
    )
    assert env.tt_days(h["id"]) == [20261123]
    assert env.ot.tasks_[oid]["project_id"] is None  # left where it is
    env.quiet()


def test_definition_changes(env: Env) -> None:
    hid = env.tt.add_habit("Read")
    env.run()
    env.tt.habits_[hid] |= {"name": "Read 20 pages", "reminders": ["07:00"]}
    res = env.run()
    assert res.habits_ot.updated == 1, res.summary()
    habit = env.ot_habit("Read 20 pages")
    assert habit["reminders"] == [-120]
    env.quiet()

    habit["day_part"] = "afternoon"  # moved in OpenTodo
    res = env.run()
    assert res.habits_tt.updated == 1, res.summary()
    assert env.tt.habits_[hid]["sectionId"] == "s-a"
    env.quiet()


def test_state_reset_pairs_habits_and_the_habits_project_again(env: Env, tmp_path: Path) -> None:
    """After a reset (README: then --match-existing), every habit was created again on both
    sides, and TickTick habits can't be deleted through the API. The old "Habits" project
    became a TickTick list."""
    read = env.tt.add_habit("Read")
    env.tt.add_habit("Walk")
    env.tt.checkins[(read, 20261123)] = 2
    env.run()
    env.state.close()
    env.state = State(tmp_path / "reset.db")
    Syncer(env.tt, env.ot, env.state, now_ms=lambda: NOW).match_existing(dry_run=False)
    res = env.run()
    assert not res.habits_tt.created and not res.habits_ot.created, res.summary()
    assert len(env.tt.habits_) == 2
    assert sum(1 for t in env.ot.tasks_.values() if t.get("habit")) == 2
    assert not env.tt.projects_
    assert env.tt_days(read) == [20261123]
    env.quiet()


def test_same_habit_made_in_both_apps_is_paired(env: Env) -> None:
    env.tt.add_habit("Stretch")
    env.ot.add_task("stretch", None, habit=True, recurrence=EVERY_DAY, due={"date": "2026-11-24"})
    res = env.run()
    assert not res.habits_tt.created and not res.habits_ot.created, res.summary()
    assert len(env.tt.habits_) == 1
    assert env.ot_habit("Stretch")  # TickTick's definition won, name included
    env.quiet()


def test_ambiguous_habit_names_are_left_alone(env: Env) -> None:
    env.tt.add_habit("Read")
    env.tt.add_habit("Read")
    env.ot.add_task("Read", None, habit=True, recurrence=EVERY_DAY, due={"date": "2026-11-24"})
    res = env.run()
    assert not res.habits_tt.created and not res.habits_ot.created, res.summary()
    assert len(env.tt.habits_) == 2
    env.quiet()


def test_clearing_many_check_ins_blocks_the_pass(env: Env) -> None:
    """The habit guard counted as an error, so --status and the summary never said BLOCKED."""
    hid = env.tt.add_habit("Read")
    for stamp in range(20261110, 20261122):  # 12 days
        env.tt.checkins[(hid, stamp)] = 2
    env.run()
    habit = env.ot_habit("Read")
    for day in env.ot_days(habit):  # un-checked in OpenTodo
        env.ot.log_habit(habit["id"], day, "clear", None)
    res = Syncer(env.tt, env.ot, env.state, now_ms=lambda: NOW).run(dry_run=False)
    assert res.blocked and "12 check-ins" in res.blocked and not res.errors
    assert "BLOCKED" in res.summary() and env.state.get_meta_json("blocked")
    assert len(env.tt_days(hid)) == 12  # none cleared in TickTick


def test_unwritable_state_creates_no_habit(env: Env) -> None:
    """A habit created without its pair recorded would be created again every pass, and
    TickTick habits can't be deleted through the API."""
    env.run()  # records the inbox id: from here on only the habit writes anything
    env.tt.add_habit("Read")
    deny_writes(env.state)
    with pytest.raises(sqlite3.DatabaseError):
        env.run()
    assert not env.ot.writes and not env.tt.writes


def test_unsupported_habit_is_warned_about_once(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """The habit sync is rebuilt every pass, so "once per process" meant every 5 minutes."""
    env.tt.add_habit("Water", type="Real", goal=8.0)
    syncer = Syncer(env.tt, env.ot, env.state, now_ms=lambda: NOW)  # one process, two passes
    syncer.run(dry_run=False)
    syncer.run(dry_run=False)
    assert caplog.text.count("TickTick habit 'Water' not synced") == 1


def test_unsupported_and_archived_habits_are_left_alone(env: Env) -> None:
    env.tt.add_habit("Water", type="Real", goal=8.0)
    env.tt.add_habit("Old", status=1)
    res = env.run()
    assert not res.changed, res.summary()
    assert not any(t.get("habit") for t in env.ot.tasks_.values())


def test_dry_run_writes_nothing(env: Env) -> None:
    hid = env.tt.add_habit("Read")
    env.tt.checkins[(hid, 20261124)] = 2
    res = env.run(dry_run=True)
    assert res.habits_ot.created == 1 and res.habits_ot.completed == 1, res.summary()
    assert not env.ot.writes and not env.tt.writes


def test_placeholder_name_is_repaired_never_copied(env: Env) -> None:
    """2026-11-24: an open browser saved the create placeholder over a habit."""
    hid = env.tt.add_habit("Drink water")
    env.run()
    env.ot_habit("Drink water")["title"] = PLACEHOLDER
    res = env.run()
    assert res.habits_ot.updated == 1 and not res.habits_tt.updated, res.summary()
    assert env.ot_habit("Drink water")
    assert env.tt.habits_[hid]["name"] == "Drink water"
    assert not [w for w in env.tt.writes if w[0] == "update_habit"]
    env.quiet()


def test_placeholder_habits_project_name_is_repaired(env: Env) -> None:
    """2026-11-24: the browser race also hit the "Habits" project's name."""
    env.tt.add_habit("Read")
    env.run()
    pid = env.state.get_meta(META_PROJECT)
    assert pid
    env.ot.projects_[pid]["name"] = PLACEHOLDER
    env.run(dry_run=True)
    assert env.ot.projects_[pid]["name"] == PLACEHOLDER
    env.run()
    assert env.ot.projects_[pid]["name"] == "Habits"
    assert not [p for p in env.tt.projects_.values() if p["name"] in ("Habits", PLACEHOLDER)]
    env.quiet()


def test_skipped_today_is_left_alone(env: Env) -> None:
    hid = env.tt.add_habit("Run")
    env.tt.checkins[(hid, 20261120)] = 2
    env.run()
    habit = env.ot_habit("Run")
    assert habit["due"] == {"date": "2026-11-24"}
    env.ot.log_habit(habit["id"], "2026-11-24", "skipped", None)
    assert habit["due"] == {"date": "2026-11-25"}
    env.quiet()  # skipped stays in OpenTodo, and the habit isn't pulled back to today
    assert habit["due"] == {"date": "2026-11-25"}
    assert env.tt_days(hid) == [20261120]
