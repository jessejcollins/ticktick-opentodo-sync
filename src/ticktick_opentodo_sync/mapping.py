"""Field mapping between the two APIs and the normalized :class:`Task`.

See docs/api-notes.md ("Field mapping") for the table this implements.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import recurrence
from .models import INBOX, JSON, PENDING_PREFIX, TICKTICK_ONLY_PREFIX, Due, Priority, Task

log = logging.getLogger(__name__)

# --- priority -------------------------------------------------------------------------------

_TT_TO_PRI = {
    0: Priority.NONE,
    1: Priority.LOW,
    2: Priority.LOW,
    3: Priority.MEDIUM,
    4: Priority.MEDIUM,
    5: Priority.HIGH,
}
_PRI_TO_TT = {Priority.NONE: 0, Priority.LOW: 1, Priority.MEDIUM: 3, Priority.HIGH: 5}
_OT_TO_PRI = {4: Priority.NONE, 3: Priority.LOW, 2: Priority.MEDIUM, 1: Priority.HIGH}
_PRI_TO_OT = {v: k for k, v in _OT_TO_PRI.items()}


def priority_from_tt(value: object) -> Priority:
    return _TT_TO_PRI.get(value if isinstance(value, int) else 0, Priority.NONE)


def priority_to_tt(p: Priority) -> int:
    return _PRI_TO_TT[p]


def priority_from_ot(value: object) -> Priority:
    return _OT_TO_PRI.get(value if isinstance(value, int) else 4, Priority.NONE)


def priority_to_ot(p: Priority) -> int:
    return _PRI_TO_OT[p]


# --- text -----------------------------------------------------------------------------------


def norm_title(s: object) -> str:
    return str(s or "").strip()


def norm_notes(s: object) -> str:
    text = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def norm_tags(tags: object) -> frozenset[str]:
    if not isinstance(tags, list):
        return frozenset()
    return frozenset(t.strip().lower() for t in tags if isinstance(t, str) and t.strip())


# --- dates ----------------------------------------------------------------------------------

TT_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")


@cache
def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown time zone %r, using UTC", name)
        return ZoneInfo("UTC")


def parse_tt_datetime(s: str) -> datetime:
    for fmt in TT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised TickTick datetime {s!r}")


def format_tt_datetime(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def due_from_tt(raw: JSON, default_tz: str) -> Due | None:
    s = raw.get("dueDate")
    if not s:
        return None
    tz_name = raw.get("timeZone") or default_tz
    local = parse_tt_datetime(s).astimezone(zone(tz_name))
    if raw.get("isAllDay"):
        return Due(local.date().isoformat())
    return Due(local.date().isoformat(), local.strftime("%H:%M"), tz_name)


def due_to_tt(due: Due | None, all_day_tz: str) -> JSON:
    """TickTick fields for ``due``. ``all_day_tz`` anchors all-day and floating dates."""
    if due is None:
        # UNVERIFIED: that null clears the date on update (docs/api-notes.md).
        return {"dueDate": None, "isAllDay": False}
    tz_name = due.tz or all_day_tz
    d = date.fromisoformat(due.date)
    if due.time is None:
        local = datetime.combine(d, time(0), zone(tz_name))
    else:
        hh, mm = (int(x) for x in due.time.split(":"))
        local = datetime.combine(d, time(hh, mm), zone(tz_name))
    return {"dueDate": format_tt_datetime(local), "isAllDay": due.time is None, "timeZone": tz_name}


def due_from_ot(raw: object, default_tz: str) -> Due | None:
    if not isinstance(raw, dict) or not raw.get("date"):
        return None
    t = raw.get("time")
    if not t:
        return Due(raw["date"])
    return Due(raw["date"], t, raw.get("tz") or default_tz)


def due_to_ot(due: Due | None) -> JSON | None:
    return due.to_json() if due else None


# --- recurrence -----------------------------------------------------------------------------


def recurrence_from_tt(raw: JSON, due: Due | None) -> str | None:
    """OpenTodo rule for a TickTick task, ``ticktick-only:...`` if inexpressible, else None."""
    flag = raw.get("repeatFlag")
    if not flag:
        return None
    t = recurrence.from_ticktick(str(flag), raw.get("repeatFrom"), _due_date(due))
    if t.rule is None:
        return f"{TICKTICK_ONLY_PREFIX}{flag};FROM={raw.get('repeatFrom')}"
    return t.rule


def _due_date(due: Due | None) -> date | None:
    return date.fromisoformat(due.date) if due else None


def recurrence_to_tt(task: Task, raw: JSON | None) -> JSON:
    """TickTick ``repeatFlag``/``repeatFrom`` to write, or {} to leave the current rule alone.

    TickTick's own spelling is kept whenever it already means the same rule, and a rule
    OpenTodo can't express (``ticktick-only``) is never cleared from the OpenTodo side.
    """
    current = recurrence_from_tt(raw or {}, task.due)
    if task.recurrence == current or task.ticktick_only_rule:
        return {}
    if task.recurrence is None:
        if current is not None and current.startswith(TICKTICK_ONLY_PREFIX):
            return {}
        return {"repeatFlag": ""}  # verified 2026-11-22: "" clears the rule
    try:
        flag, frm = recurrence.to_ticktick(task.recurrence)
    except recurrence.Untranslatable as exc:
        log.warning("leaving the TickTick rule alone: %s", exc)
        return {}
    return {"repeatFlag": flag, "repeatFrom": frm}


# --- reminders ------------------------------------------------------------------------------
# TickTick: iCalendar TRIGGER durations from the due time, or from midnight for an all-day
# task ("TRIGGER:P0DT9H0M0S" is 09:00 on the day). OpenTodo (and Task.reminders): minutes from
# the due time, or from 09:00 for a date-only due. So all-day offsets differ by 540.

ALL_DAY_ANCHOR = 9 * 60
_TRIGGER = re.compile(
    r"TRIGGER:([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
    re.IGNORECASE,
)


def parse_trigger(text: str) -> int | None:
    """Signed minutes of a TickTick ``TRIGGER:`` duration, or None if not one."""
    m = _TRIGGER.fullmatch(text.strip())
    if m is None or text.strip().upper() in ("TRIGGER:P", "TRIGGER:-P", "TRIGGER:+P"):
        return None
    w, d, h, mi, sec = (int(g or 0) for g in m.groups()[1:])
    minutes = ((w * 7 + d) * 24 + h) * 60 + mi + sec // 60
    return -minutes if m.group(1) == "-" else minutes


def format_trigger(minutes: int, *, all_day: bool) -> str:
    """TickTick's own spelling: ``P0DT9H0M0S`` style for all-day tasks, ``-PT30M`` otherwise."""
    sign = "-" if minutes < 0 else ""
    d, rest = divmod(abs(minutes), 24 * 60)
    h, mi = divmod(rest, 60)
    if all_day:
        return f"TRIGGER:{sign}P{d}DT{h}H{mi}M0S"
    if not minutes:
        return "TRIGGER:PT0S"
    time_part = (f"{h}H" if h else "") + (f"{mi}M" if mi else "")
    return f"TRIGGER:{sign}P{f'{d}D' if d else ''}{'T' + time_part if time_part else ''}"


def _tt_reminders(raw: JSON, all_day: bool) -> tuple[dict[int, str], list[str]]:
    """TickTick's reminders as offset -> its spelling, plus the ones that aren't triggers."""
    known: dict[int, str] = {}
    other: list[str] = []
    for r in raw.get("reminders") or []:
        minutes = parse_trigger(str(r))
        if minutes is None:
            other.append(str(r))
        else:
            known.setdefault(minutes - ALL_DAY_ANCHOR if all_day else minutes, str(r))
    return known, other


def reminders_from_tt(raw: JSON, due: Due | None) -> frozenset[int]:
    return frozenset(_tt_reminders(raw, due is not None and due.time is None)[0])


def reminders_to_tt(task: Task, raw: JSON | None) -> JSON:
    """TickTick ``reminders`` to write, or {} to leave them as they are.

    TickTick's spelling of a reminder is kept while it still means the same, and entries
    that aren't triggers are never dropped.
    """
    raw = raw or {}
    was_all_day = bool(raw.get("isAllDay")) if raw.get("dueDate") else False
    all_day = task.due is not None and task.due.time is None
    known, other = _tt_reminders(raw, was_all_day)
    if was_all_day != all_day:
        known = {}
    elif set(known) == set(task.reminders):
        return {}
    wanted = [
        known.get(m) or format_trigger(m + ALL_DAY_ANCHOR if all_day else m, all_day=all_day)
        for m in sorted(task.reminders)
    ]
    return {"reminders": wanted + other}


# --- project reconciliation -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    name: str
    active: bool


@dataclass
class ProjectPlan:
    pairs: dict[str, str] = field(default_factory=dict)
    """tt_id -> ot_id, including new name matches and projects being restored."""
    new_pairs: list[tuple[str, str, str]] = field(default_factory=list)
    """(tt_id, ot_id, name) matched by name this run; persist them."""
    stale: list[str] = field(default_factory=list)
    """tt_ids of stored pairs to forget now: the OpenTodo project is gone (the TickTick list
    is then matched or created again: TickTick wins), or both are, or the list is gone and
    its project is inactive already."""
    retire: list[tuple[str, str]] = field(default_factory=list)
    """(tt_id, ot_id): the TickTick list was deleted. Its active OpenTodo project is archived
    once its tasks went through the delete rules, never recreated as a list."""
    restore: list[tuple[str, str]] = field(default_factory=list)
    """(tt_id, ot_id): the project is in OpenTodo's Trash while its list lives. Deleting a
    project only hides its tasks, so restoring it brings them back (TickTick wins)."""
    create_in_ot: list[tuple[str, str]] = field(default_factory=list)
    """(tt_id, name)."""
    create_in_tt: list[tuple[str, str]] = field(default_factory=list)
    """(ot_id, name)."""


def reconcile_projects(
    stored: Mapping[str, str],
    tt: Mapping[str, ProjectInfo],
    ot: Mapping[str, ProjectInfo],
    trashed: AbstractSet[str] = frozenset(),
) -> ProjectPlan:
    """Pair lists and projects: by id once paired, by name otherwise.

    ``trashed``: ids of OpenTodo projects listed with ``deleted_at`` set (not in ``ot``).
    """
    plan = ProjectPlan()
    retired: set[str] = set()
    for tt_id, ot_id in stored.items():
        if tt_id in tt and ot_id in ot:
            plan.pairs[tt_id] = ot_id
        elif tt_id in tt and ot_id in trashed:
            plan.pairs[tt_id] = ot_id
            plan.restore.append((tt_id, ot_id))
        elif tt_id not in tt and ot_id in ot and ot[ot_id].active:
            plan.retire.append((tt_id, ot_id))
            retired.add(ot_id)
        else:
            plan.stale.append(tt_id)

    taken = set(plan.pairs.values()) | retired
    ot_by_name: dict[str, str] = {}
    # Active projects first: a name an archived project shares goes to the active one.
    for ot_id, info in sorted(ot.items(), key=lambda kv: (not kv[1].active, kv[1].name)):
        if ot_id in taken:
            continue
        key = info.name.strip().casefold()
        if key in ot_by_name:
            if info.active:
                log.warning(
                    "duplicate OpenTodo project name %r; matching only the first", info.name
                )
            continue
        ot_by_name[key] = ot_id

    for tt_id, info in sorted(tt.items(), key=lambda kv: kv[1].name):
        if tt_id in plan.pairs:
            continue
        key = info.name.strip().casefold()
        match = ot_by_name.get(key)
        # A live list never pairs with an archived project: its tasks would never sync.
        if match is not None and (ot[match].active or not info.active):
            del ot_by_name[key]
            plan.pairs[tt_id] = match
            plan.new_pairs.append((tt_id, match, info.name))
        elif info.active:
            plan.create_in_ot.append((tt_id, info.name))

    for ot_id in ot_by_name.values():
        if ot[ot_id].active:
            plan.create_in_tt.append((ot_id, ot[ot_id].name))
    return plan


# --- whole tasks ----------------------------------------------------------------------------

TT_DONE = frozenset({2, -1})  # Completed, Won't Do
TT_DROP_ON_MERGE = ("etag", "focusSummaries", "modifiedTime", "createdTime")


@dataclass
class Mapper:
    """Converts raw API dicts to :class:`Task` and back, given the current id mappings."""

    default_tz: str
    tt_inbox_id: str | None = None
    tt_to_ot_project: dict[str, str] = field(default_factory=dict)
    label_names: dict[str, str] = field(default_factory=dict)
    """OpenTodo label id -> lowercased name."""
    tt_to_ot_task: dict[str, str] = field(default_factory=dict)
    ot_to_tt_task: dict[str, str] = field(default_factory=dict)
    """Paired task ids, for subtask parents."""

    def link_task(self, tt_id: str, ot_id: str) -> None:
        self.tt_to_ot_task[tt_id] = ot_id
        self.ot_to_tt_task[ot_id] = tt_id

    def ot_task_id(self, key: str) -> str:
        """OpenTodo id of a parent task key (see :attr:`Task.parent`)."""
        if key.startswith(PENDING_PREFIX):
            return key[len(PENDING_PREFIX) :]
        try:
            return self.tt_to_ot_task[key]
        except KeyError:
            raise ValueError(f"parent task {key} has no OpenTodo twin yet") from None

    @property
    def ot_to_tt_project(self) -> dict[str, str]:
        return {v: k for k, v in self.tt_to_ot_project.items()}

    # TickTick ---------------------------------------------------------------------------

    def tt_project_key(self, project_id: str) -> str:
        if project_id == self.tt_inbox_id or project_id == INBOX:
            return INBOX
        return project_id

    def tt_project_id(self, key: str) -> str:
        if key == INBOX:
            return self.tt_inbox_id or INBOX
        if key.startswith(PENDING_PREFIX):
            raise ValueError(f"project {key} has no TickTick list yet")
        return key

    def from_tt(self, raw: JSON) -> Task:
        notes = raw.get("desc") if raw.get("kind") == "CHECKLIST" else raw.get("content")
        due = due_from_tt(raw, self.default_tz)
        return Task(
            title=norm_title(raw.get("title")),
            project=self.tt_project_key(str(raw.get("projectId") or INBOX)),
            notes=norm_notes(notes),
            due=due,
            priority=priority_from_tt(raw.get("priority")),
            tags=norm_tags(raw.get("tags")),
            completed=raw.get("status") in TT_DONE,
            recurrence=recurrence_from_tt(raw, due),
            parent=str(raw["parentId"]) if raw.get("parentId") else None,
            reminders=reminders_from_tt(raw, due),
        )

    def to_tt(self, task: Task, raw: JSON | None = None) -> JSON:
        """Full TickTick body for ``task``, merged over ``raw`` (the current task) if given.

        Completion is not set here; the caller completes/reopens explicitly.
        """
        out: JSON = {k: v for k, v in (raw or {}).items() if k not in TT_DROP_ON_MERGE}
        out["title"] = task.title
        out["projectId"] = self.tt_project_id(task.project)
        out["priority"] = priority_to_tt(task.priority)
        out["tags"] = sorted(task.tags)
        out["desc" if out.get("kind") == "CHECKLIST" else "content"] = task.notes

        all_day_tz = (raw or {}).get("timeZone") or self.default_tz
        old_due, old_start = (raw or {}).get("dueDate"), (raw or {}).get("startDate")
        out |= due_to_tt(task.due, all_day_tz)
        new_due = out.get("dueDate")
        if new_due is None:
            out["startDate"] = None
        elif (
            raw is None
            or not old_start
            or old_start == old_due
            or parse_tt_datetime(old_start) > parse_tt_datetime(new_due)
        ):
            out["startDate"] = new_due
        out |= recurrence_to_tt(task, raw)
        out |= reminders_to_tt(task, raw)
        if task.parent and not task.parent.startswith(PENDING_PREFIX):
            out["parentId"] = task.parent
        return out

    # OpenTodo ---------------------------------------------------------------------------

    def ot_project_key(self, project_id: str | None) -> str:
        if project_id is None:
            return INBOX
        tt_id = self.ot_to_tt_project.get(project_id)
        return tt_id if tt_id is not None else PENDING_PREFIX + project_id

    def ot_project_id(self, key: str) -> str | None:
        if key == INBOX:
            return None
        if key.startswith(PENDING_PREFIX):
            return key[len(PENDING_PREFIX) :]
        try:
            return self.tt_to_ot_project[key]
        except KeyError:
            raise ValueError(f"TickTick list {key} has no OpenTodo project yet") from None

    def from_ot(self, raw: JSON) -> Task:
        labels = raw.get("labels") or []
        return Task(
            title=norm_title(raw.get("title")),
            project=self.ot_project_key(raw.get("project_id")),
            notes=norm_notes(raw.get("description")),
            due=due_from_ot(raw.get("due"), self.default_tz),
            priority=priority_from_ot(raw.get("priority")),
            tags=frozenset(self.label_names[i] for i in labels if i in self.label_names),
            completed=raw.get("completed_at") is not None,
            recurrence=str(raw["recurrence"]) if raw.get("recurrence") else None,
            parent=self._ot_parent_key(raw.get("parent_id")),
            reminders=frozenset(int(m) for m in raw.get("reminders") or ()),
        )

    def _ot_parent_key(self, parent_id: object) -> str | None:
        if not parent_id:
            return None
        return self.ot_to_tt_task.get(str(parent_id)) or PENDING_PREFIX + str(parent_id)

    def to_ot(
        self,
        task: Task,
        label_ids: Mapping[str, str],
        *,
        currently_completed: bool | None,
        now_ms: int,
    ) -> JSON:
        """OpenTodo create/PATCH body. ``label_ids`` maps lowercased name -> id."""
        out: JSON = {
            "title": task.title,
            "description": task.notes,
            "due": due_to_ot(task.due),
            "priority": priority_to_ot(task.priority),
            "labels": sorted(label_ids[t] for t in task.tags if t in label_ids),
            "project_id": self.ot_project_id(task.project),
            "reminders": sorted(task.reminders),  # the client reconciles these
        }
        if not task.ticktick_only_rule:
            out["recurrence"] = task.recurrence
        if task.parent:
            out["parent_id"] = self.ot_task_id(task.parent)
        if task.completed and not currently_completed:
            out["completed_at"] = now_ms
            # A REST completion of a repeating task only advances it; TickTick says it's done.
            out["recurrence"] = None
        elif not task.completed and currently_completed:
            out["completed_at"] = None
        return out


# --- checklist items <-> OpenTodo subtasks ----------------------------------------------------
# An item is a Task with only title and completed set; ``project`` holds the TickTick parent id.

TT_ITEM_DONE = 1


def item_from_tt(item: JSON, parent: str) -> Task:
    return Task(norm_title(item.get("title")), parent, completed=bool(item.get("status")))


def item_from_ot(raw: JSON, parent: str) -> Task:
    return Task(norm_title(raw.get("title")), parent, completed=raw.get("completed_at") is not None)
