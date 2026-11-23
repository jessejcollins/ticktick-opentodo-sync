from __future__ import annotations

from dataclasses import replace

import pytest

from ticktick_opentodo_sync.mapping import (
    Mapper,
    ProjectInfo,
    due_from_ot,
    due_from_tt,
    due_to_tt,
    format_trigger,
    norm_notes,
    parse_trigger,
    priority_from_ot,
    priority_from_tt,
    priority_to_ot,
    priority_to_tt,
    reconcile_projects,
    reminders_from_tt,
    reminders_to_tt,
)
from ticktick_opentodo_sync.models import INBOX, Due, Priority, Task

NY = "America/New_York"


@pytest.mark.parametrize(
    ("tt", "ot", "pri"),
    [(0, 4, Priority.NONE), (1, 3, Priority.LOW), (3, 2, Priority.MEDIUM), (5, 1, Priority.HIGH)],
)
def test_priority_mapping_round_trips(tt: int, ot: int, pri: Priority) -> None:
    assert priority_from_tt(tt) is pri and priority_to_tt(pri) == tt
    assert priority_from_ot(ot) is pri and priority_to_ot(pri) == ot


def test_priority_odd_values() -> None:
    assert priority_from_tt(2) is Priority.LOW
    assert priority_from_tt(4) is Priority.MEDIUM
    assert priority_from_tt(None) is Priority.NONE
    assert priority_from_ot(None) is Priority.NONE


def test_tt_all_day_due_uses_task_zone() -> None:
    # Midnight 14 Nov in Los Angeles is 08:00 UTC.
    raw = {
        "dueDate": "2019-11-14T08:00:00+0000",
        "isAllDay": True,
        "timeZone": "America/Los_Angeles",
    }
    assert due_from_tt(raw, NY) == Due("2019-11-14")


def test_tt_timed_due_with_millis() -> None:
    raw = {"dueDate": "2026-03-04T23:58:20.000+0000", "isAllDay": False, "timeZone": NY}
    assert due_from_tt(raw, "UTC") == Due("2026-03-04", "18:58", NY)


def test_tt_due_missing_zone_falls_back_to_default() -> None:
    raw = {"dueDate": "2026-01-01T15:00:00+0000", "isAllDay": False}
    assert due_from_tt(raw, NY) == Due("2026-01-01", "10:00", NY)


def test_due_to_tt_round_trip() -> None:
    for due in (Due("2026-07-04"), Due("2026-07-04", "09:30", NY), Due("2026-11-01", "01:30", NY)):
        fields = due_to_tt(due, NY)
        assert due_from_tt(fields, NY) == due


def test_due_to_tt_clear() -> None:
    assert due_to_tt(None, NY) == {"dueDate": None, "isAllDay": False}


def test_ot_floating_time_gets_default_zone() -> None:
    assert due_from_ot({"date": "2026-11-20", "time": "10:00"}, NY) == Due(
        "2026-11-20", "10:00", NY
    )
    assert due_from_ot({"date": "2026-11-20"}, NY) == Due("2026-11-20")
    assert due_from_ot(None, NY) is None


def test_norm_notes() -> None:
    assert norm_notes("a  \r\nb\t\r\n\n") == "a\nb"
    assert norm_notes(None) == ""


def test_reconcile_projects() -> None:
    tt = {
        "t1": ProjectInfo("Work", True),
        "t2": ProjectInfo("home", True),
        "t3": ProjectInfo("Old", False),
        "t4": ProjectInfo("Kept", True),
    }
    ot = {
        "o1": ProjectInfo("Home", True),
        "o2": ProjectInfo("Garden", True),
        "o3": ProjectInfo("Archived", False),
        "o4": ProjectInfo("Kept", True),
    }
    plan = reconcile_projects({"t4": "o4", "gone": "o9"}, tt, ot)
    assert plan.pairs == {"t4": "o4", "t2": "o1"}
    assert plan.new_pairs == [("t2", "o1", "home")]
    assert plan.stale == ["gone"]
    assert plan.create_in_ot == [("t1", "Work")]  # closed "Old" is not created
    assert plan.create_in_tt == [("o2", "Garden")]  # archived is not created


def make_mapper() -> Mapper:
    return Mapper(
        default_tz=NY,
        tt_inbox_id="inbox1",
        tt_to_ot_project={"ttp": "otp"},
        label_names={"l1": "errands"},
    )


def test_from_tt_and_to_tt() -> None:
    m = make_mapper()
    raw = {
        "id": "x",
        "projectId": "inbox1",
        "title": " Paint ",
        "content": "two litres\r\n",
        "dueDate": "2026-11-20T05:00:00+0000",
        "isAllDay": True,
        "timeZone": NY,
        "priority": 3,
        "status": 0,
        "tags": ["Errands"],
        "repeatFlag": "RRULE:FREQ=DAILY",
        "etag": "abc",
        "kind": "TEXT",
        "startDate": "2026-11-20T05:00:00+0000",
    }
    task = m.from_tt(raw)
    assert task == Task(
        "Paint",
        INBOX,
        "two litres",
        Due("2026-11-20"),
        Priority.MEDIUM,
        frozenset({"errands"}),
        recurrence="every day",
    )
    out = m.to_tt(task, raw)
    assert out["projectId"] == "inbox1" and out["repeatFlag"] == "RRULE:FREQ=DAILY"
    assert "etag" not in out and out["startDate"] == out["dueDate"]
    assert m.from_tt(out) == task


def test_to_tt_keeps_start_of_date_range() -> None:
    m = make_mapper()
    raw = {
        "id": "x",
        "projectId": "ttp",
        "title": "Trip",
        "isAllDay": True,
        "timeZone": NY,
        "startDate": "2026-11-18T05:00:00+0000",
        "dueDate": "2026-11-20T05:00:00+0000",
    }
    out = m.to_tt(Task("Trip", "ttp", due=Due("2026-11-21")), raw)
    assert out["startDate"] == raw["startDate"]
    out = m.to_tt(Task("Trip", "ttp", due=Due("2026-11-10")), raw)
    assert out["startDate"] == out["dueDate"]


def test_checklist_notes_use_desc() -> None:
    m = make_mapper()
    raw = {"projectId": "ttp", "title": "List", "kind": "CHECKLIST", "desc": "d", "content": "c"}
    assert m.from_tt(raw).notes == "d"
    assert m.to_tt(Task("List", "ttp", notes="new"), raw)["desc"] == "new"


def test_from_ot_and_to_ot() -> None:
    m = make_mapper()
    raw = {
        "id": "o",
        "title": "Paint",
        "description": "x",
        "project_id": "otp",
        "priority": 1,
        "due": {"date": "2026-11-20", "time": "10:00"},
        "labels": ["l1", "unknown"],
        "completed_at": None,
    }
    task = m.from_ot(raw)
    assert task == Task(
        "Paint", "ttp", "x", Due("2026-11-20", "10:00", NY), Priority.HIGH, frozenset({"errands"})
    )
    body = m.to_ot(task, {"errands": "l1"}, currently_completed=False, now_ms=5)
    assert body == {
        "title": "Paint",
        "description": "x",
        "project_id": "otp",
        "priority": 1,
        "due": {"date": "2026-11-20", "time": "10:00", "tz": NY},
        "labels": ["l1"],
        "recurrence": None,
        "reminders": [],
    }


def test_to_ot_completion_transitions() -> None:
    m = make_mapper()
    done = Task("a", completed=True)
    assert m.to_ot(done, {}, currently_completed=False, now_ms=7)["completed_at"] == 7
    assert "completed_at" not in m.to_ot(done, {}, currently_completed=True, now_ms=7)
    assert m.to_ot(Task("a"), {}, currently_completed=True, now_ms=7)["completed_at"] is None


def test_ot_inbox_and_pending_projects() -> None:
    m = make_mapper()
    assert m.ot_project_key(None) == INBOX
    assert m.ot_project_key("new") == "ot:new"
    assert m.ot_project_id("ot:new") == "new"
    with pytest.raises(ValueError, match="no TickTick list"):
        m.tt_project_id("ot:new")


# --- recurrence (phase 2) --------------------------------------------------------------------


def test_recurrence_to_tt_keeps_ticktick_spelling_and_untranslatable_rules() -> None:
    m = make_mapper()
    raw = {
        "projectId": "ttp",
        "title": "Review",
        "isAllDay": True,
        "timeZone": NY,
        "dueDate": "2026-11-23T05:00:00+0000",  # a Monday
        "repeatFlag": "RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=1;BYDAY=MO",
        "repeatFrom": "0",
    }
    task = m.from_tt(raw)
    assert task.recurrence == "every monday"
    out = m.to_tt(task, raw)
    assert out["repeatFlag"] == raw["repeatFlag"]  # same rule: TickTick's spelling kept

    out = m.to_tt(replace(task, recurrence="every 2 weeks after completion"), raw)
    assert (out["repeatFlag"], out["repeatFrom"]) == ("RRULE:FREQ=WEEKLY;INTERVAL=2", "1")
    assert m.to_tt(replace(task, recurrence=None), raw)["repeatFlag"] == ""

    odd = raw | {"repeatFlag": "RRULE:FREQ=MONTHLY;BYMONTHDAY=6,10"}
    t = m.from_tt(odd)
    assert t.ticktick_only_rule
    # OpenTodo can't hold it, so its "no rule" never clears TickTick's rule.
    assert m.to_tt(replace(t, recurrence=None), odd)["repeatFlag"] == odd["repeatFlag"]
    assert "recurrence" not in m.to_ot(t, {}, currently_completed=False, now_ms=1)


def test_recurrence_to_ot_and_back() -> None:
    m = make_mapper()
    task = Task("Pay rent", "ttp", due=Due("2026-12-01"), recurrence="every 1st")
    body = m.to_ot(task, {}, currently_completed=False, now_ms=1)
    assert body["recurrence"] == "every 1st"
    raw = {
        "project_id": "otp",
        "title": "Pay rent",
        "recurrence": "every 1st",
        "due": {"date": "2026-12-01"},
    }
    assert m.from_ot(raw) == task
    done = m.to_ot(replace(task, completed=True), {}, currently_completed=False, now_ms=1)
    assert done["completed_at"] == 1 and done["recurrence"] is None


# --- reminders ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trigger", "minutes"),
    [
        ("TRIGGER:PT0S", 0),
        ("TRIGGER:-PT0S", 0),
        ("TRIGGER:-PT30M", -30),
        ("TRIGGER:-PT1H", -60),
        ("TRIGGER:-P1D", -1440),
        ("TRIGGER:-P1DT2H5M", -1565),
        ("TRIGGER:P0DT9H0M0S", 540),
        ("TRIGGER:-P0DT15H0M0S", -900),
        ("TRIGGER:-P1W", -10080),
        ("trigger:pt5m", 5),
    ],
)
def test_parse_trigger(trigger: str, minutes: int) -> None:
    assert parse_trigger(trigger) == minutes


@pytest.mark.parametrize("text", ["", "TRIGGER:", "TRIGGER:P", "TRIGGER:19700101T000000Z", "x"])
def test_parse_trigger_rejects(text: str) -> None:
    assert parse_trigger(text) is None


@pytest.mark.parametrize(
    ("minutes", "all_day", "want"),
    [
        (0, False, "TRIGGER:PT0S"),
        (-30, False, "TRIGGER:-PT30M"),
        (-90, False, "TRIGGER:-PT1H30M"),
        (-1440, False, "TRIGGER:-P1D"),
        (-2940, False, "TRIGGER:-P2DT1H"),
        (540, True, "TRIGGER:P0DT9H0M0S"),
        (420, True, "TRIGGER:P0DT7H0M0S"),
        (-900, True, "TRIGGER:-P0DT15H0M0S"),
    ],
)
def test_format_trigger(minutes: int, all_day: bool, want: str) -> None:
    assert format_trigger(minutes, all_day=all_day) == want
    assert parse_trigger(want) == minutes


ALL_DAY: dict[str, object] = {"dueDate": "2026-11-25T05:00:00+0000", "isAllDay": True}
TIMED: dict[str, object] = {"dueDate": "2026-11-25T14:00:00+0000", "isAllDay": False}


def test_reminders_from_tt_count_all_day_from_nine() -> None:
    due_day, due_time = Due("2026-11-25"), Due("2026-11-25", "09:00", NY)
    raw = ALL_DAY | {"reminders": ["TRIGGER:P0DT7H0M0S", "TRIGGER:-P0DT15H0M0S"]}
    assert reminders_from_tt(raw, due_day) == {-120, -1440}
    raw = TIMED | {"reminders": ["TRIGGER:-PT0S", "TRIGGER:-PT30M", "odd"]}
    assert reminders_from_tt(raw, due_time) == {0, -30}


def test_reminders_to_tt_keeps_spelling_and_unknown_entries() -> None:
    due = Due("2026-11-25", "09:00", NY)
    raw = TIMED | {"reminders": ["TRIGGER:-PT0S", "odd"]}
    task = Task("x", due=due, reminders=frozenset({0}))
    assert reminders_to_tt(task, raw) == {}
    task = Task("x", due=due, reminders=frozenset({0, -30}))
    assert reminders_to_tt(task, raw) == {"reminders": ["TRIGGER:-PT30M", "TRIGGER:-PT0S", "odd"]}
    assert reminders_to_tt(Task("x", due=due), raw) == {"reminders": ["odd"]}


def test_reminders_to_tt_all_day() -> None:
    task = Task("x", due=Due("2026-11-25"), reminders=frozenset({-120, -1440}))
    assert reminders_to_tt(task, None) == {
        "reminders": ["TRIGGER:-P0DT15H0M0S", "TRIGGER:P0DT7H0M0S"]
    }
    # Switching a timed task to all-day rewrites the triggers for the new anchor.
    raw = TIMED | {"reminders": ["TRIGGER:PT0S"]}
    task = Task("x", due=Due("2026-11-25"), reminders=frozenset({0}))
    assert reminders_to_tt(task, raw) == {"reminders": ["TRIGGER:P0DT9H0M0S"]}
