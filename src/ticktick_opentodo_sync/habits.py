"""Pure habit sync: definitions and daily check-ins. No I/O.

A TickTick habit is its own object with daily check-ins; an OpenTodo habit is a repeating task
flagged ``habit`` whose days are occurrence records. Definitions (name, rule, part of the day,
reminder times) sync like tasks: per-side hashes, TickTick wins. A day is either done or not on
each side; one boolean of state per day (``base``) tells which side changed it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from . import recurrence
from .models import JSON

REMINDER_ANCHOR = 9 * 60
"""OpenTodo counts a date-only due's relative reminders from 09:00."""

DAY_PARTS = ("morning", "afternoon", "evening")
TT_SECTION_PARTS = {"_morning": "morning", "_afternoon": "afternoon", "_night": "evening"}
"""TickTick's built-in habit sections."""
PART_TT_SECTIONS = {v: k for k, v in TT_SECTION_PARTS.items()}

TT_DONE = 2
"""Check-in status: 0 unmarked, 1 not completed, 2 completed."""
TT_UNMARKED = 0
TT_ACTIVE = 0
"""Habit status of a habit in use (archived ones are left alone)."""

_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


@dataclass(frozen=True, slots=True)
class HabitDef:
    """One habit's definition, independent of either API."""

    name: str
    rule: str | None = None
    """OpenTodo rule (canonical English), as for tasks."""
    day_part: str | None = None
    """``morning``, ``afternoon``, ``evening`` or None."""
    reminders: frozenset[int] = field(default_factory=frozenset)
    """Minutes from 09:00 on the day (OpenTodo relative reminders on a date-only due)."""

    def to_json(self) -> JSON:
        return {
            "name": self.name,
            "rule": self.rule,
            "day_part": self.day_part,
            "reminders": sorted(self.reminders),
        }

    @classmethod
    def from_json(cls, data: JSON) -> HabitDef:
        return cls(
            name=data["name"],
            rule=data.get("rule"),
            day_part=data.get("day_part"),
            reminders=frozenset(data.get("reminders", ())),
        )

    def content_hash(self) -> str:
        blob = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class HabitPair:
    tt_id: str
    ot_id: str
    tt_hash: str
    ot_hash: str
    base: HabitDef | None = None


# --- mapping ----------------------------------------------------------------------------------


class Unsupported(ValueError):
    """A habit this version doesn't sync (amounts, targets, untranslatable rules)."""


def habit_from_tt(raw: JSON, sections: Mapping[str, str]) -> HabitDef:
    """``sections``: TickTick section id -> name."""
    kind = str(raw.get("type") or "Boolean")
    if kind != "Boolean" or float(raw.get("goal") or 1) != 1:
        raise Unsupported(f"a {kind} habit with goal {raw.get('goal')} (only yes/no habits sync)")
    flag = str(raw.get("repeatRule") or "")
    if "TT_TIMES" in flag.upper() or int(raw.get("targetDays") or 0):
        raise Unsupported("a habit with a target ('N times a week' or 'N days')")
    rule = None
    if flag:
        t = recurrence.from_ticktick(flag, 0, None)
        if t.rule is None:
            raise Unsupported(f"repeat rule {flag!r}: {t.problem}")
        rule = t.rule
    reminders: set[int] = set()
    for r in raw.get("reminders") or []:
        m = _HHMM.fullmatch(str(r).strip())
        if m is None:
            raise Unsupported(f"reminder {r!r}")
        reminders.add(int(m.group(1)) * 60 + int(m.group(2)) - REMINDER_ANCHOR)
    return HabitDef(
        name=str(raw.get("name") or "").strip(),
        rule=rule,
        day_part=TT_SECTION_PARTS.get(sections.get(str(raw.get("sectionId") or ""), "")),
        reminders=frozenset(reminders),
    )


def habit_to_tt(habit: HabitDef, raw: JSON | None, section_ids: Mapping[str, str]) -> JSON:
    """TickTick body: the changed fields over ``raw``, or a whole new yes/no habit.

    ``section_ids``: TickTick section name -> id. TickTick's spelling of an equivalent rule
    is kept.
    """
    out: JSON = {"name": habit.name}
    current = raw or {}
    flag = str(current.get("repeatRule") or "")
    same_rule = (bool(flag) and recurrence.from_ticktick(flag, 0, None).rule == habit.rule) or (
        not flag and habit.rule is None
    )
    if not same_rule:
        out["repeatRule"] = recurrence.to_ticktick(habit.rule)[0] if habit.rule else ""
    section = section_ids.get(PART_TT_SECTIONS.get(habit.day_part or "", ""))
    if section and section != current.get("sectionId"):
        out["sectionId"] = section
    out["reminders"] = [_hhmm(m) for m in sorted(habit.reminders) if _hhmm(m)]
    if raw is None:
        out |= {"type": "Boolean", "goal": 1.0, "step": 1.0, "unit": "Count", "status": 0}
    return out


def _hhmm(offset: int) -> str:
    minutes = offset + REMINDER_ANCHOR
    if not 0 <= minutes < 24 * 60:
        return ""  # another day: TickTick habit reminders are times of the day
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def habit_from_ot(raw: JSON) -> HabitDef:
    part = "evening" if raw.get("evening") else raw.get("day_part")
    return HabitDef(
        name=str(raw.get("title") or "").strip(),
        rule=str(raw["recurrence"]) if raw.get("recurrence") else None,
        day_part=str(part) if part in DAY_PARTS else None,
        reminders=frozenset(int(m) for m in raw.get("reminders") or ()),
    )


def habit_to_ot(habit: HabitDef) -> JSON:
    """OpenTodo task fields of a habit (``reminders`` is reconciled by the client)."""
    out: JSON = {
        "title": habit.name,
        "recurrence": habit.rule,
        "habit": True,
        "reminders": sorted(habit.reminders),
    }
    if habit.day_part == "evening":
        out |= {"evening": True, "day_part": None}
    else:
        out |= {"evening": False, "day_part": habit.day_part}
    return out


def stamp_to_day(stamp: object) -> str:
    s = str(stamp)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def day_to_stamp(day: str) -> int:
    return int(day.replace("-", ""))


def tt_done_days(docs: Iterable[JSON]) -> set[tuple[str, str]]:
    """(TickTick habit id, day) checked in, from ``GET /open/v1/habit/checkins``."""
    out: set[tuple[str, str]] = set()
    for doc in docs:
        latest: dict[str, JSON] = {}
        for c in doc.get("checkins") or []:
            if c.get("stamp") is None:
                continue
            day = stamp_to_day(c["stamp"])
            if day not in latest or str(c.get("opTime") or "") >= str(
                latest[day].get("opTime") or ""
            ):
                latest[day] = c
        out |= {
            (str(doc.get("habitId")), day)
            for day, c in latest.items()
            if c.get("status") == TT_DONE
        }
    return out


def ot_done_days(records: Iterable[JSON]) -> set[tuple[str, str]]:
    """(OpenTodo habit id, day) done or tiny, from its live occurrence records."""
    return {
        (str(r["series_id"]), str(r["occurrence_date"]))
        for r in records
        if r.get("series_id")
        and r.get("occurrence_date")
        and r.get("completed_at") is not None
        and not r.get("skipped")
        and r.get("deleted_at") is None
    }


# --- planning ---------------------------------------------------------------------------------


class HKind(Enum):
    CREATE_OT = "create_ot"
    CREATE_TT = "create_tt"
    UPDATE_OT = "update_ot"
    UPDATE_TT = "update_tt"
    REHASH = "rehash"


@dataclass(frozen=True, slots=True)
class HabitAction:
    kind: HKind
    habit: HabitDef
    tt_id: str | None = None
    ot_id: str | None = None
    conflict: bool = False
    note: str = ""


def plan_habits(
    pairs: Iterable[HabitPair], tt: Mapping[str, HabitDef], ot: Mapping[str, HabitDef]
) -> list[HabitAction]:
    """Definitions. A habit missing on one side is left alone: deletions aren't synced."""
    out: list[HabitAction] = []
    pairs = list(pairs)
    for p in sorted(pairs, key=lambda p: p.tt_id):
        t, o = tt.get(p.tt_id), ot.get(p.ot_id)
        if t is None or o is None:
            continue
        t_changed, o_changed = t.content_hash() != p.tt_hash, o.content_hash() != p.ot_hash
        if not (t_changed or o_changed):
            continue
        if t == o:
            out.append(HabitAction(HKind.REHASH, t, p.tt_id, p.ot_id, note="same on both sides"))
        elif t_changed:
            note = "conflict: TickTick wins" if o_changed else "changed in TickTick"
            out.append(HabitAction(HKind.UPDATE_OT, t, p.tt_id, p.ot_id, o_changed, note))
        else:
            out.append(
                HabitAction(HKind.UPDATE_TT, o, p.tt_id, p.ot_id, note="changed in OpenTodo")
            )
    paired_tt, paired_ot = {p.tt_id for p in pairs}, {p.ot_id for p in pairs}
    for tt_id, h in sorted(tt.items()):
        if tt_id not in paired_tt:
            out.append(HabitAction(HKind.CREATE_OT, h, tt_id=tt_id, note="new in TickTick"))
    for ot_id, h in sorted(ot.items()):
        if ot_id not in paired_ot:
            out.append(HabitAction(HKind.CREATE_TT, h, ot_id=ot_id, note="new in OpenTodo"))
    return out


def match_habits(
    pairs: Iterable[HabitPair], tt: Mapping[str, HabitDef], ot: Mapping[str, HabitDef]
) -> tuple[list[tuple[str, str]], set[str]]:
    """Unpaired habits with the same name on both sides: pair them, don't copy each over.

    After a state reset every habit is unpaired, and copying them would duplicate each one
    on both sides (TickTick habits can't be deleted through the API). Returns the
    (TickTick id, OpenTodo id) matches, and the names (casefolded) on both sides that aren't
    unique on one of them: ambiguous, so neither side's copies are made.
    """
    pairs = list(pairs)
    paired_tt, paired_ot = {p.tt_id for p in pairs}, {p.ot_id for p in pairs}
    tt_names: dict[str, list[str]] = {}
    for tt_id, h in sorted(tt.items()):
        if tt_id not in paired_tt:
            tt_names.setdefault(name_key(h.name), []).append(tt_id)
    ot_names: dict[str, list[str]] = {}
    for ot_id, h in sorted(ot.items()):
        if ot_id not in paired_ot:
            ot_names.setdefault(name_key(h.name), []).append(ot_id)
    matches: list[tuple[str, str]] = []
    ambiguous: set[str] = set()
    for key in sorted(tt_names.keys() & ot_names.keys()):
        if len(tt_names[key]) == 1 and len(ot_names[key]) == 1:
            matches.append((tt_names[key][0], ot_names[key][0]))
        else:
            ambiguous.add(key)
    return matches, ambiguous


def name_key(name: str) -> str:
    return name.strip().casefold()


class DKind(Enum):
    CHECK_OT = "check_ot"
    CLEAR_OT = "clear_ot"
    CHECK_TT = "check_tt"
    CLEAR_TT = "clear_tt"
    RECORD = "record"
    """Both sides agree on a new state: remember it, write nothing."""


@dataclass(frozen=True, slots=True)
class DayAction:
    kind: DKind
    tt_id: str
    ot_id: str
    day: str
    done: bool


def plan_days(
    pairs: Iterable[tuple[str, str]],
    days: Sequence[str],
    tt_done: set[tuple[str, str]],
    ot_done: set[tuple[str, str]],
    base: set[tuple[str, str]],
    *,
    refused: AbstractSet[tuple[str, str]] = frozenset(),
) -> list[DayAction]:
    """Check-ins of (TickTick id, OpenTodo id) pairs on ``days`` (oldest first).

    With one boolean per side, the sides can only disagree when exactly one of them moved
    away from ``base``, so there is no conflict to resolve. A new pair has an empty base:
    the side that has check-ins copies them over (the first-run backfill).

    ``refused``: (TickTick id, day) check-ins OpenTodo would not record (a day before the
    habit's first one there). They stay in TickTick only and aren't offered again; they
    never enter ``base``, so the gap in OpenTodo doesn't read as an un-check there.
    """
    out: list[DayAction] = []
    for tt_id, ot_id in pairs:
        for day in days:
            t, o, b = (tt_id, day) in tt_done, (ot_id, day) in ot_done, (tt_id, day) in base
            if t == o:
                if t != b:
                    out.append(DayAction(DKind.RECORD, tt_id, ot_id, day, t))
            elif t != b:
                if t and (tt_id, day) in refused:
                    continue
                out.append(DayAction(DKind.CHECK_OT if t else DKind.CLEAR_OT, tt_id, ot_id, day, t))
            else:
                out.append(DayAction(DKind.CHECK_TT if o else DKind.CLEAR_TT, tt_id, ot_id, day, o))
    return out


def window(today: date, n: int) -> list[str]:
    """The last ``n`` days up to ``today``, oldest first."""
    return [date.fromordinal(today.toordinal() - i).isoformat() for i in range(n - 1, -1, -1)]
