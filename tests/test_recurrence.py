from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeOpenTodo, FakeTickTick

from ticktick_opentodo_sync.recurrence import (
    Untranslatable,
    from_rrule,
    from_ticktick,
    parse_canonical,
    to_ticktick,
)
from ticktick_opentodo_sync.state import State
from ticktick_opentodo_sync.sync import Syncer

TUE = date(2026, 10, 13)

# OpenTodo internal/recurrence/rrule_test.go TestFromRRULEClientSpellings (due 2026-10-13).
OPENTODO_ACCEPTS = {
    "FREQ=WEEKLY;INTERVAL=1;WKST=MO": "every week",
    "freq=weekly;byday=TU;interval=2": "every 2 weeks",
    "FREQ=WEEKLY;INTERVAL=2;BYDAY=SA": "every 2 weeks on saturday",
    "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE": "every 2 weeks on monday, wednesday",
    "FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=13": "every 3 months",
    "FREQ=YEARLY;BYMONTH=10;BYMONTHDAY=13": "every year",
    "FREQ=DAILY;UNTIL=20261231T225959Z": "every day until 2026-12-31",
    "FREQ=WEEKLY;BYDAY=FR,MO,TH,TU,WE": "every weekday",
    "FREQ=MONTHLY;BYDAY=+1MO": "every 1st monday",
    "FREQ=MONTHLY;BYSETPOS=-1;BYDAY=MO,TU,WE,TH,FR": "every last weekday",
    "FREQ=MONTHLY;BYSETPOS=+3;BYDAY=FR,TH,WE,TU,MO": "every 3rd weekday",
}
OPENTODO_REFUSES = [
    "FREQ=HOURLY",
    "FREQ=DAILY;BYHOUR=9",
    "FREQ=MONTHLY;BYDAY=5MO",
    "FREQ=MONTHLY;BYDAY=1MO,3MO",
    "FREQ=YEARLY;BYMONTH=3",
    "FREQ=DAILY;COUNT=2;UNTIL=20261231",
    "FREQ=SECONDLY",
    "",
    "FREQ=DAILY;INTERVAL=0",
    "FREQ=MONTHLY;BYMONTHDAY=40",
    "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=5",
    "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2",
    "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1,2",
    "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH;BYSETPOS=1",
    "FREQ=MONTHLY;INTERVAL=2;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1",
]


@pytest.mark.parametrize(("rrule", "want"), OPENTODO_ACCEPTS.items())
def test_accepts_like_opentodo(rrule: str, want: str) -> None:
    assert from_rrule(rrule, TUE).canonical() == want


@pytest.mark.parametrize("rrule", OPENTODO_REFUSES)
def test_refuses_like_opentodo(rrule: str) -> None:
    with pytest.raises(Untranslatable):
        from_rrule(rrule, TUE)


@pytest.mark.parametrize(
    ("flag", "repeat_from", "due", "want"),
    [
        ("RRULE:FREQ=DAILY", 2, date(2026, 11, 22), "every day"),
        ("RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=1;BYDAY=MO", 0, date(2026, 11, 23), "every monday"),
        (
            "RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=1;BYDAY=TH,SU",
            0,
            date(2026, 11, 26),
            "every thursday, sunday",
        ),
        (
            "RRULE:FREQ=YEARLY;INTERVAL=1;BYMONTH=11;BYDAY=-1SA",
            0,
            date(2026, 11, 28),
            "every last saturday of november",
        ),
        ("RRULE:FREQ=MONTHLY;INTERVAL=6;BYMONTHDAY=1", 0, date(2027, 1, 1), "every 6 months"),
        ("RRULE:FREQ=MONTHLY;BYMONTHDAY=-1", 0, date(2026, 11, 30), "every last day"),
        (
            "RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=2",
            1,
            date(2026, 12, 1),
            "every 2 weeks after completion",
        ),
        ("RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=4;BYDAY=SU", 0, date(2026, 11, 29), "every 4 weeks"),
        (
            "RRULE:FREQ=WEEKLY;WKST=SU;INTERVAL=4;BYDAY=SU",
            0,
            date(2026, 11, 28),
            "every 4 weeks on sunday",
        ),
    ],
)
def test_ticktick_rules(flag: str, repeat_from: int, due: date, want: str) -> None:
    t = from_ticktick(flag, repeat_from, due)
    assert t.rule == want


def test_ticktick_untranslatable_and_warnings() -> None:
    assert from_ticktick("RRULE:FREQ=MONTHLY;BYMONTHDAY=6,10", 0, date(2026, 12, 6)).rule is None
    assert from_ticktick("RRULE:FREQ=MONTHLY;INTERVAL=6;BYDAY=1SA", 0, None).rule is None
    t = from_ticktick("RRULE:FREQ=WEEKLY;BYDAY=MO", 1, date(2026, 11, 23))
    assert t.rule is None and "completion" in t.problem
    t = from_ticktick("RRULE:FREQ=WEEKLY;BYDAY=MO", 0, date(2026, 11, 24))
    assert t.rule == "every monday" and "tuesday" in t.problem
    t = from_ticktick("RRULE:FREQ=MONTHLY;INTERVAL=1;BYDAY=1SU", 0, date(2026, 12, 6))
    assert t.rule == "every 1st sunday" and not t.problem


def test_report_writes_nothing(tmp_path: Path) -> None:
    tt, ot = FakeTickTick(), FakeOpenTodo()
    state = State(tmp_path / "s.db")
    syncer = Syncer(tt, ot, state)
    due: dict[str, Any] = {
        "dueDate": "2026-11-23T05:00:00+0000",
        "isAllDay": True,
        "timeZone": "America/New_York",
    }
    tt.add_task("Weekly review", repeatFlag="RRULE:FREQ=WEEKLY;BYDAY=MO", repeatFrom=0, **due)
    tt.add_task("Odd", repeatFlag="RRULE:FREQ=MONTHLY;BYMONTHDAY=6,10", repeatFrom=0, **due)
    tt.add_task("Plain")
    assert syncer.run(dry_run=False).ok
    tt.writes.clear()
    ot.writes.clear()

    rep = syncer.recurrence_report()
    assert dict(rep.translated) == {("RRULE:FREQ=WEEKLY;BYDAY=MO from=0", "every monday"): 1}
    assert [u[0] for u in rep.untranslatable] == ["'Odd' in 'Inbox' (due 2026-11-23)"]
    assert not tt.writes and not ot.writes
    assert "2 repeating paired task(s): 1 translate, 1 don't" in rep.lines()[0]
    state.close()


@pytest.mark.parametrize(
    "rule",
    [
        "every day",
        "every 3 days",
        "every week",
        "every 2 weeks",
        "every month",
        "every 6 months",
        "every year",
        "every weekday",
        "every monday",
        "every wednesday, saturday, sunday",
        "every 4 weeks on saturday",
        "every 2 weeks on monday, wednesday",
        "every 1st",
        "every 22nd",
        "every last day",
        "every 3rd weekday",
        "every last weekday",
        "every 1st sunday",
        "every last friday",
        "every 1st saturday of november",
        "every last sunday of december",
        "every 2 weeks after completion",
        "every day until 2026-12-31",
        "every weekday for 10 times",
        "every month for 1 time",
    ],
)
def test_opentodo_rules_round_trip(rule: str) -> None:
    parsed = parse_canonical(rule)
    assert parsed.canonical() == rule
    flag, repeat_from = to_ticktick(rule)
    assert from_ticktick(flag, repeat_from, None).rule == rule


def test_unknown_opentodo_rules() -> None:
    for bad in ["every fortnight", "every 5th monday", "monday", "every monday after completion"]:
        with pytest.raises(Untranslatable):
            parse_canonical(bad)
