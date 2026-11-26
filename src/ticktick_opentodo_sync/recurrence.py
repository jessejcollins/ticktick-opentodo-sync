"""TickTick repeat rules (iCalendar RRULE + ``repeatFrom``) -> OpenTodo ``recurrence`` phrases.

A port of OpenTodo's ``recurrence.FromRRULE``, ``Rule.Compatible`` and ``Rule.Canonical``
(``internal/recurrence``, checked 2026-11-22), the converter its CalDAV server uses, so a rule
translates exactly when OpenTodo itself would accept it. Anything it can't express exactly is
reported as untranslatable instead of approximated.

Used both ways by the task mapping (see CLAUDE.md, "Recurrence" in the decisions log) and by
the habit sync for habits' repeat rules.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

MAX_INTERVAL = 999
MAX_COUNT = 10_000
RR_DAYS = ("", "MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAYS = ("", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("", *(m.lower() for m in calendar.month_name[1:]))
WORKDAYS = frozenset(range(1, 6))

# TickTick repeatFrom: 0 = from the due date, 1 = from the completion date, 2 = calendar
# (docs/api-notes.md). 0 and 2 both advance from the schedule, not from completion.
TT_FROM_COMPLETION = 1


class Kind(Enum):
    INTERVAL = "interval"
    WEEKDAY = "weekday"  # Monday-Friday
    WEEKDAYS = "weekdays"  # a set of weekdays, optionally every N weeks
    MONTH_DAY = "month day"
    LAST_DAY = "last day"
    WORKDAY = "nth workday"  # 1st-4th / last Monday-Friday of the month
    NTH_WEEKDAY = "nth weekday"  # 1st-4th / last <weekday> of each month (or of one month)


@dataclass(frozen=True, slots=True)
class Rule:
    kind: Kind
    interval: int = 1
    unit: str = ""  # day/week/month/year, for INTERVAL
    days: frozenset[int] = frozenset()  # ISO weekdays 1-7
    month_day: int = 0
    ordinal: int = 0  # 1-4 or -1
    weekday: int = 0
    month: int = 0
    after_completion: bool = False
    until: str = ""
    count: int = 0

    def canonical(self) -> str:
        """OpenTodo's stored form (``Rule.Canonical``)."""
        out = "every "
        match self.kind:
            case Kind.INTERVAL:
                out += self.unit if self.interval == 1 else f"{self.interval} {self.unit}s"
            case Kind.WEEKDAY:
                out += "weekday"
            case Kind.WEEKDAYS:
                if self.interval > 1:
                    out += f"{self.interval} weeks on "
                out += ", ".join(WEEKDAYS[d] for d in sorted(self.days))
            case Kind.MONTH_DAY:
                out += _ordinal(self.month_day)
            case Kind.LAST_DAY:
                out += "last day"
            case Kind.WORKDAY:
                out += ("last" if self.ordinal < 0 else _ordinal(self.ordinal)) + " weekday"
            case Kind.NTH_WEEKDAY:
                out += "last" if self.ordinal < 0 else _ordinal(self.ordinal)
                out += " " + WEEKDAYS[self.weekday]
                if self.month:
                    out += " of " + MONTHS[self.month]
        if self.after_completion:
            out += " after completion"
        if self.until:
            out += " until " + self.until
        if self.count:
            out += f" for {self.count} time" + ("s" if self.count > 1 else "")
        return out

    def compatible(self, due: date | None) -> Rule:
        """``every N weeks on <day>`` on a task due that day is stored as ``every N weeks``."""
        if (
            self.kind is Kind.WEEKDAYS
            and self.interval > 1
            and due is not None
            and self.days == {due.isoweekday()}
        ):
            return Rule(Kind.INTERVAL, self.interval, "week", until=self.until, count=self.count)
        return self

    def on_pattern(self, d: date) -> bool:
        """Does ``d`` fall on one of the rule's dates? (Interval rules: always.)"""
        last = calendar.monthrange(d.year, d.month)[1]
        match self.kind:
            case Kind.INTERVAL:
                return True
            case Kind.WEEKDAY:
                return d.isoweekday() in WORKDAYS
            case Kind.WEEKDAYS:
                return d.isoweekday() in self.days
            case Kind.MONTH_DAY:
                return d.day == min(self.month_day, last)
            case Kind.LAST_DAY:
                return d.day == last
            case Kind.WORKDAY:
                work = [x for x in range(1, last + 1) if date(d.year, d.month, x).isoweekday() <= 5]
                return d.day == work[self.ordinal - 1 if self.ordinal > 0 else -1]
            case Kind.NTH_WEEKDAY:
                if self.month and d.month != self.month:
                    return False
                same = [
                    x
                    for x in range(1, last + 1)
                    if date(d.year, d.month, x).isoweekday() == self.weekday
                ]
                return d.day == same[self.ordinal - 1 if self.ordinal > 0 else -1]


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


class Untranslatable(ValueError):
    pass


def _parts(rrule: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in rrule.strip().removeprefix("RRULE:").split(";"):
        if not part.strip():
            continue
        k, sep, v = part.partition("=")
        if not sep:
            raise Untranslatable(f"RRULE part {part!r} has no value")
        k, v = k.strip().upper(), v.strip().upper()
        if k in out:
            raise Untranslatable(f"RRULE repeats {k}")
        out[k] = v
    out.pop("WKST", None)
    if out.get("INTERVAL") == "1":
        del out["INTERVAL"]
    if len(out.get("UNTIL", "")) > 8:
        out["UNTIL"] = out["UNTIL"][:8]
    if "BYDAY" in out:
        out["BYDAY"] = ",".join(sorted(out["BYDAY"].split(",")))
    return out


def _positive(s: str, maximum: int) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", s) or int(s) > maximum:
        raise Untranslatable(f"{s!r} is not a number from 1 to {maximum}")
    return int(s)


def _day_set(s: str) -> frozenset[int]:
    try:
        return frozenset(RR_DAYS.index(d) for d in s.split(",") if d and RR_DAYS.index(d))
    except ValueError:
        raise Untranslatable(f"BYDAY={s} has an ordinal or an unknown day") from None


def _ordinal_day(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"([+-]?\d+)(MO|TU|WE|TH|FR|SA|SU)", s)
    if not m or not (int(m[1]) == -1 or 1 <= int(m[1]) <= 4):
        raise Untranslatable(f"BYDAY={s}: OpenTodo knows only 1st-4th and last")
    return int(m[1]), RR_DAYS.index(m[2])


def from_rrule(rrule: str, due: date | None) -> Rule:
    """OpenTodo rule for an RRULE, or :class:`Untranslatable` (``recurrence.FromRRULE``)."""
    p = _parts(rrule)
    unknown = set(p) - {
        "FREQ",
        "INTERVAL",
        "UNTIL",
        "COUNT",
        "BYDAY",
        "BYMONTHDAY",
        "BYSETPOS",
        "BYMONTH",
    }
    if unknown:
        raise Untranslatable(f"unsupported RRULE part(s): {', '.join(sorted(unknown))}")
    interval = _positive(p["INTERVAL"], MAX_INTERVAL) if "INTERVAL" in p else 1
    until = ""
    if "UNTIL" in p:
        u = p["UNTIL"]
        try:
            until = date(int(u[:4]), int(u[4:6]), int(u[6:8])).isoformat()
        except ValueError:
            raise Untranslatable(f"UNTIL={u} is not a date") from None
    count = 0
    if "COUNT" in p:
        if until:
            raise Untranslatable("both UNTIL and COUNT")
        count = _positive(p["COUNT"], MAX_COUNT)
    byday, monthday, setpos, bymonth = (
        p.get(k) for k in ("BYDAY", "BYMONTHDAY", "BYSETPOS", "BYMONTH")
    )
    freq = p.get("FREQ")

    def plain(unit: str) -> Rule:
        return Rule(Kind.INTERVAL, interval, unit, until=until, count=count)

    if freq == "DAILY":
        if byday or monthday or setpos or bymonth:
            raise Untranslatable("daily rule with BY* parts")
        return plain("day")
    if freq == "WEEKLY":
        if monthday or setpos or bymonth:
            raise Untranslatable("weekly rule with month parts")
        if not byday:
            return plain("week")
        days = _day_set(byday)
        if not days:
            raise Untranslatable("empty BYDAY")
        if interval > 1:
            return Rule(Kind.WEEKDAYS, interval, days=days, until=until, count=count).compatible(
                due
            )
        if days == WORKDAYS:
            return Rule(Kind.WEEKDAY, days=days, until=until, count=count)
        return Rule(Kind.WEEKDAYS, days=days, until=until, count=count)
    if freq == "MONTHLY":
        if bymonth:
            raise Untranslatable("monthly rule with BYMONTH")
        if not (byday or monthday or setpos):
            return plain("month")
        if monthday and not byday and not setpos:
            if not re.fullmatch(r"-?\d+", monthday):
                raise Untranslatable(f"BYMONTHDAY={monthday}: OpenTodo knows one day per month")
            n = int(monthday)
            if interval > 1:
                if due is not None and n == due.day:
                    return plain("month")
                raise Untranslatable(f"every {interval} months on day {n}, but due on another day")
            if n == -1:
                return Rule(Kind.LAST_DAY, until=until, count=count)
            if 1 <= n <= 31:
                return Rule(Kind.MONTH_DAY, month_day=n, until=until, count=count)
            raise Untranslatable(f"BYMONTHDAY={monthday}")
        if byday and not monthday and interval == 1:
            if setpos:
                pos = int(setpos) if re.fullmatch(r"[+-]?\d+", setpos) else 0
                if not (pos == -1 or 1 <= pos <= 4) or _day_set(byday) != WORKDAYS:
                    raise Untranslatable(f"BYSETPOS={setpos} with BYDAY={byday}")
                return Rule(Kind.WORKDAY, ordinal=pos, until=until, count=count)
            if "," in byday:
                raise Untranslatable(f"BYDAY={byday}: several ordinal weekdays")
            ordinal, wd = _ordinal_day(byday)
            return Rule(Kind.NTH_WEEKDAY, ordinal=ordinal, weekday=wd, until=until, count=count)
        raise Untranslatable(
            "ordinal weekday every N months" if byday and interval > 1 else "monthly combination"
        )
    if freq == "YEARLY":
        if byday and bymonth and not setpos and not monthday and interval == 1:
            if "," in byday or "," in bymonth or not bymonth.isdigit():
                raise Untranslatable("several weekdays or months")
            ordinal, wd = _ordinal_day(byday)
            month = int(bymonth)
            if not 1 <= month <= 12:
                raise Untranslatable(f"BYMONTH={bymonth}")
            return Rule(
                Kind.NTH_WEEKDAY, ordinal=ordinal, weekday=wd, month=month, until=until, count=count
            )
        if byday or setpos:
            raise Untranslatable("yearly rule with BYDAY/BYSETPOS")
        if bymonth and (due is None or bymonth != str(due.month)):
            raise Untranslatable(f"BYMONTH={bymonth} differs from the due date's month")
        if monthday and (due is None or monthday != str(due.day)):
            raise Untranslatable(f"BYMONTHDAY={monthday} differs from the due date's day")
        return plain("year")
    raise Untranslatable(f"FREQ={freq} is not supported")


@dataclass(frozen=True, slots=True)
class Translation:
    rule: str | None
    """OpenTodo ``recurrence`` value, or ``None`` when untranslatable."""
    problem: str = ""
    """Why it is untranslatable, or a warning about a translated rule."""


def from_ticktick(repeat_flag: str, repeat_from: object, due: date | None) -> Translation:
    """TickTick ``repeatFlag`` + ``repeatFrom`` for a task due on ``due`` (its local date)."""
    try:
        rule = from_rrule(repeat_flag, due)
    except Untranslatable as exc:
        return Translation(None, str(exc))
    if str(repeat_from) == str(TT_FROM_COMPLETION):
        if rule.kind is not Kind.INTERVAL:
            return Translation(None, "repeats from completion with a calendar pattern")
        rule = replace(rule, after_completion=True)
    warning = ""
    if due is None:
        warning = "no due date: OpenTodo would pick the first matching date"
    elif not rule.on_pattern(due):
        warning = f"due {due.isoformat()} ({WEEKDAYS[due.isoweekday()]}) is not on the rule"
    return Translation(rule.canonical(), warning)


# --- OpenTodo -> TickTick ---------------------------------------------------------------------

_UNITS = ("day", "week", "month", "year")
_FREQ = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}
_ORD = r"(?:(\d+)(?:st|nd|rd|th)|(last))"
_DAY_NAMES = "|".join(WEEKDAYS[1:])


def _weekday_set(text: str) -> frozenset[int]:
    names = [n.strip() for n in text.split(",")]
    if not names or any(n not in WEEKDAYS[1:] for n in names):
        raise Untranslatable(f"not a list of weekdays: {text!r}")
    return frozenset(WEEKDAYS.index(n) for n in names)


def parse_canonical(text: str) -> Rule:
    """An OpenTodo ``recurrence`` value (its stored, canonical form) -> :class:`Rule`."""
    s = " ".join(text.lower().split())
    count, until, after = 0, "", False
    if m := re.search(r" for (\d+) times?$", s):
        count, s = int(m[1]), s[: m.start()]
    if m := re.search(r" until (\d{4}-\d{2}-\d{2})$", s):
        until, s = m[1], s[: m.start()]
    if s.endswith(" after completion"):
        after, s = True, s.removesuffix(" after completion")
    if not s.startswith("every "):
        raise Untranslatable(f"not an OpenTodo rule: {text!r}")
    body = s.removeprefix("every ")

    def ordinal(num: str | None, last: str | None) -> int:
        n = -1 if last else int(num or 0)
        if not (n == -1 or 1 <= n <= 4):
            raise Untranslatable(f"ordinal out of range in {text!r}")
        return n

    rule: Rule
    if m := re.fullmatch(rf"(?:(\d+) )?({'|'.join(_UNITS)})s?", body):
        rule = Rule(Kind.INTERVAL, int(m[1] or 1), m[2], until=until, count=count)
    elif body == "weekday":
        rule = Rule(Kind.WEEKDAY, days=WORKDAYS, until=until, count=count)
    elif m := re.fullmatch(r"(\d+) weeks on (.+)", body):
        rule = Rule(Kind.WEEKDAYS, int(m[1]), days=_weekday_set(m[2]), until=until, count=count)
    elif m := re.fullmatch(r"(\d+)(?:st|nd|rd|th)", body):
        rule = Rule(Kind.MONTH_DAY, month_day=int(m[1]), until=until, count=count)
    elif body == "last day":
        rule = Rule(Kind.LAST_DAY, until=until, count=count)
    elif m := re.fullmatch(rf"{_ORD} weekday", body):
        rule = Rule(Kind.WORKDAY, ordinal=ordinal(m[1], m[2]), until=until, count=count)
    elif m := re.fullmatch(rf"{_ORD} ({_DAY_NAMES})(?: of ([a-z]+))?", body):
        month = MONTHS.index(m[4]) if m[4] in MONTHS[1:] else (0 if m[4] is None else -1)
        if month < 0:
            raise Untranslatable(f"unknown month in {text!r}")
        rule = Rule(
            Kind.NTH_WEEKDAY,
            ordinal=ordinal(m[1], m[2]),
            weekday=WEEKDAYS.index(m[3]),
            month=month,
            until=until,
            count=count,
        )
    else:
        try:
            days = _weekday_set(body)
        except Untranslatable:
            raise Untranslatable(f"unknown OpenTodo rule {text!r}") from None
        rule = Rule(Kind.WEEKDAYS, days=days, until=until, count=count)
    if after and rule.kind is not Kind.INTERVAL:
        raise Untranslatable(f"'after completion' on a calendar pattern: {text!r}")
    return replace(rule, after_completion=after)


def _rrule(rule: Rule) -> str:
    """``Rule.RRULE`` in OpenTodo: completion-relative rules give their plain interval."""
    match rule.kind:
        case Kind.INTERVAL:
            parts = [f"FREQ={_FREQ[rule.unit]}"]
            if rule.interval > 1:
                parts.append(f"INTERVAL={rule.interval}")
        case Kind.WEEKDAY | Kind.WEEKDAYS:
            parts = ["FREQ=WEEKLY"]
            if rule.interval > 1:
                parts.append(f"INTERVAL={rule.interval}")
            parts.append("BYDAY=" + ",".join(RR_DAYS[d] for d in sorted(rule.days)))
        case Kind.MONTH_DAY:
            parts = ["FREQ=MONTHLY", f"BYMONTHDAY={rule.month_day}"]
        case Kind.LAST_DAY:
            parts = ["FREQ=MONTHLY", "BYMONTHDAY=-1"]
        case Kind.WORKDAY:
            parts = ["FREQ=MONTHLY", "BYDAY=MO,TU,WE,TH,FR", f"BYSETPOS={rule.ordinal}"]
        case Kind.NTH_WEEKDAY:
            by_day = f"BYDAY={rule.ordinal}{RR_DAYS[rule.weekday]}"
            parts = (
                ["FREQ=YEARLY", f"BYMONTH={rule.month}", by_day]
                if rule.month
                else ["FREQ=MONTHLY", by_day]
            )
    if rule.until:
        parts.append("UNTIL=" + rule.until.replace("-", ""))
    if rule.count:
        parts.append(f"COUNT={rule.count}")
    return "RRULE:" + ";".join(parts)


def to_ticktick(opentodo_rule: str) -> tuple[str, str]:
    """OpenTodo rule -> TickTick (``repeatFlag``, ``repeatFrom``)."""
    rule = parse_canonical(opentodo_rule)
    return _rrule(rule), str(TT_FROM_COMPLETION if rule.after_completion else 0)
