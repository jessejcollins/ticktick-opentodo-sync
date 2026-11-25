"""In-memory stand-ins for both APIs, faithful to the behaviour in docs/api-notes.md."""

from __future__ import annotations

import copy
import itertools
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ticktick_opentodo_sync.http import ApiError
from ticktick_opentodo_sync.mapping import format_tt_datetime, parse_tt_datetime
from ticktick_opentodo_sync.models import JSON
from ticktick_opentodo_sync.state import State

_ids = itertools.count(1)

_WRITES = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})


def deny_writes(state: State) -> None:
    """Make every state write fail, like a full disk or a failing SD card (``None`` undoes)."""

    def authorizer(action: int, *_: object) -> int:
        return sqlite3.SQLITE_DENY if action in _WRITES else sqlite3.SQLITE_OK

    state.db.set_authorizer(authorizer)


def _id(prefix: str) -> str:
    return f"{prefix}{next(_ids):04d}"


class FakeTickTick:
    def __init__(self, tz: str = "America/New_York") -> None:
        self.tz = tz
        self.inbox_id = "inbox100"
        self.projects_: dict[str, JSON] = {}
        self.tasks: dict[str, JSON] = {}
        self.writes: list[tuple[str, Any]] = []
        self.habits_: dict[str, JSON] = {}
        self.checkins: dict[tuple[str, int], int] = {}

    # helpers for tests
    def add_habit(self, name: str, **fields: Any) -> str:
        hid = _id("hb")
        self.habits_[hid] = {
            "id": hid,
            "name": name,
            "status": 0,
            "type": "Boolean",
            "goal": 1.0,
            "repeatRule": "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=SU,MO,TU,WE,TH,FR,SA",
            "reminders": [],
            "sectionId": "s-m",
            "targetDays": 0,
        } | fields
        return hid

    def add_project(self, name: str, *, closed: bool = False) -> str:
        pid = _id("ttp")
        self.projects_[pid] = {"id": pid, "name": name, "closed": closed, "kind": "TASK"}
        return pid

    def add_task(self, title: str, project: str | None = None, **fields: Any) -> str:
        tid = _id("tt")
        self.tasks[tid] = {
            "id": tid,
            "projectId": project or self.inbox_id,
            "title": title,
            "content": "",
            "priority": 0,
            "status": 0,
            "tags": [],
            "kind": "TEXT",
        } | fields
        return tid

    # API
    def preference_timezone(self) -> str | None:
        return self.tz

    def projects(self) -> list[JSON]:
        return [copy.deepcopy(p) for p in self.projects_.values()]

    def project_data(self, project_id: str) -> JSON:
        pid = self.inbox_id if project_id == "inbox" else project_id
        tasks = [
            copy.deepcopy(t)
            for t in self.tasks.values()
            if t["projectId"] == pid and t["status"] == 0
        ]
        return {"project": {"id": pid}, "tasks": tasks, "columns": []}

    def _in(self, project_id: str, task_id: str) -> JSON | None:
        """The task, only when asked for under its own list: whether the real API answers a
        lookup under another list is UNVERIFIED (docs/api-notes.md), so assume a 404."""
        t = self.tasks.get(task_id)
        pid = self.inbox_id if project_id == "inbox" else project_id
        return t if t is not None and t["projectId"] == pid else None

    def get_task(self, project_id: str, task_id: str) -> JSON | None:
        t = self._in(project_id, task_id)
        return copy.deepcopy(t) if t else None

    @staticmethod
    def _item_ids(body: JSON) -> JSON:
        """TickTick gives new checklist items an id."""
        if isinstance(body.get("items"), list):
            body = body | {"items": [{"id": _id("it")} | i for i in body["items"]]}
        return body

    def create_task(self, body: JSON) -> JSON:
        self.writes.append(("create", body))
        body = self._item_ids(body)
        tid = _id("tt")
        pid = self.inbox_id if body["projectId"] == "inbox" else body["projectId"]
        self.tasks[tid] = {"status": 0, "kind": "TEXT"} | body | {"id": tid, "projectId": pid}
        return copy.deepcopy(self.tasks[tid])

    def update_task(self, task_id: str, body: JSON) -> JSON:
        self.writes.append(("update", body))
        self.tasks[task_id] = self.tasks[task_id] | self._item_ids(body)
        return copy.deepcopy(self.tasks[task_id])

    def complete_task(self, project_id: str, task_id: str) -> None:
        self.writes.append(("complete", task_id))
        t = self.tasks[task_id]
        if t.get("repeatFlag") and t.get("dueDate"):
            # Recurring: the task stays open and advances; a completed copy is split off.
            done = copy.deepcopy(t) | {
                "id": _id("tt"),
                "status": 2,
                "completedTime": "2026-11-23T03:00:00+0000",
            }
            self.tasks[done["id"]] = done
            nxt = parse_tt_datetime(t["dueDate"]) + timedelta(days=1)
            t["dueDate"] = t["startDate"] = format_tt_datetime(nxt)
        else:
            t["status"] = 2

    # habits: {id: raw}, sections, and check-ins {(habit id, stamp): status}
    def habits(self) -> list[JSON]:
        return [copy.deepcopy(h) for h in self.habits_.values()]

    def habit_sections(self) -> list[JSON]:
        return [
            {"id": "s-m", "name": "_morning"},
            {"id": "s-a", "name": "_afternoon"},
            {"id": "s-n", "name": "_night"},
        ]

    def habit_checkins(self, habit_ids: list[str], from_stamp: int, to_stamp: int) -> list[JSON]:
        return [
            {
                "habitId": hid,
                "checkins": [
                    {"stamp": st, "status": status, "opTime": "x"}
                    for (h, st), status in sorted(self.checkins.items())
                    if h == hid and from_stamp <= st <= to_stamp
                ],
            }
            for hid in habit_ids
        ]

    def create_habit(self, body: JSON) -> JSON:
        self.writes.append(("create_habit", body))
        hid = _id("hb")
        self.habits_[hid] = {"id": hid, "status": 0} | body
        return copy.deepcopy(self.habits_[hid])

    def update_habit(self, habit_id: str, body: JSON) -> JSON:
        self.writes.append(("update_habit", body))
        self.habits_[habit_id] |= body
        return copy.deepcopy(self.habits_[habit_id])

    def checkin(self, habit_id: str, body: JSON) -> JSON:
        self.writes.append(("checkin", (habit_id, body["stamp"], body["status"])))
        self.checkins[(habit_id, body["stamp"])] = body["status"]
        return {}

    def completed_tasks(self, project_ids: list[str], start: Any, end: Any) -> list[JSON]:
        return [
            copy.deepcopy(t)
            for t in self.tasks.values()
            if t["status"] in (2, -1) and t["projectId"] in project_ids
        ]

    def delete_task(self, project_id: str, task_id: str) -> None:
        self.writes.append(("delete", task_id))
        if self._in(project_id, task_id) is not None:  # else a 404, which the client ignores
            del self.tasks[task_id]

    def move_task(self, task_id: str, from_project: str, to_project: str) -> None:
        self.writes.append(("move", task_id))
        self.tasks[task_id]["projectId"] = to_project

    def create_project(self, name: str) -> JSON:
        self.writes.append(("create_project", name))
        pid = self.add_project(name)
        return copy.deepcopy(self.projects_[pid])


class FakeOpenTodo:
    def __init__(self) -> None:
        self.projects_: dict[str, JSON] = {}
        self.labels_: dict[str, JSON] = {}
        self.tasks_: dict[str, JSON] = {}
        self.writes: list[tuple[str, Any]] = []
        self.idempotency: dict[str, str] = {}
        self.refuse_occurrences = False
        self.today: str | None = None
        """Set by habit tests: OpenTodo's idea of today, for log_habit."""

    def add_project(self, name: str, *, archived: bool = False) -> str:
        pid = _id("otp")
        self.projects_[pid] = {"id": pid, "name": name, "archived": archived, "deleted_at": None}
        return pid

    def add_task(self, title: str, project_id: str | None = None, **fields: Any) -> str:
        oid = _id("ot")
        self.tasks_[oid] = {
            "id": oid,
            "title": title,
            "description": "",
            "project_id": project_id,
            "priority": 4,
            "due": None,
            "labels": [],
            "completed_at": None,
            "deleted_at": None,
            "parent_id": None,
            "recurrence": None,
            "reminders": [],
        } | fields
        return oid

    def live(self) -> list[JSON]:
        return [t for t in self.tasks_.values() if t["deleted_at"] is None]

    def label_name(self, label_id: str) -> str:
        return str(self.labels_[label_id]["name"])

    # API
    def begin_pass(self) -> None:
        pass

    def projects(self) -> list[JSON]:
        return [copy.deepcopy(p) for p in self.projects_.values()]

    def create_project(self, name: str) -> JSON:
        self.writes.append(("create_project", name))
        return copy.deepcopy(self.projects_[self.add_project(name)])

    def rename_project(self, project_id: str, name: str) -> JSON:
        self.writes.append(("rename_project", name))
        self.projects_[project_id]["name"] = name
        return copy.deepcopy(self.projects_[project_id])

    def archive_project(self, project_id: str) -> JSON:
        self.writes.append(("archive_project", project_id))
        self.projects_[project_id]["archived"] = True
        return copy.deepcopy(self.projects_[project_id])

    def restore_project(self, project_id: str) -> JSON:
        self.writes.append(("restore_project", project_id))
        self.projects_[project_id]["deleted_at"] = None
        return copy.deepcopy(self.projects_[project_id])

    def labels(self) -> list[JSON]:
        return [copy.deepcopy(x) for x in self.labels_.values()]

    def create_label(self, name: str) -> JSON:
        self.writes.append(("create_label", name))
        lid = _id("otl")
        self.labels_[lid] = {"id": lid, "name": name, "deleted_at": None}
        return copy.deepcopy(self.labels_[lid])

    def tasks(self) -> list[JSON]:
        return [copy.deepcopy(t) for t in self.tasks_.values()]

    def create_task(self, body: JSON, *, idempotency_key: str) -> JSON:
        self.writes.append(("create", body))
        if idempotency_key in self.idempotency:
            return copy.deepcopy(self.tasks_[self.idempotency[idempotency_key]])
        oid = self.add_task(body["title"], body.get("project_id"))
        self.tasks_[oid] |= body
        self.idempotency[idempotency_key] = oid
        return copy.deepcopy(self.tasks_[oid])

    def update_task(self, task_id: str, body: JSON) -> JSON:
        self.writes.append(("update", body))
        self.tasks_[task_id] |= body
        return copy.deepcopy(self.tasks_[task_id])

    def complete_occurrence(self, task_id: str, date: str, completed_at: int | None) -> JSON:
        """Like OpenTodo: a completed record for ``date``, and the series moves on a day."""
        self.writes.append(("complete_occurrence", (task_id, date, completed_at)))
        series = self.tasks_[task_id]
        if self.refuse_occurrences or (series.get("due") or {}).get("date") != date:
            raise ApiError("OpenTodo", "POST", "occurrence", 400, "not_an_occurrence")
        record = self.add_task(series["title"], series["project_id"])
        self.tasks_[record] |= {
            "series_id": task_id,
            "occurrence_date": date,
            "completed_at": completed_at or 1,
            "due": copy.deepcopy(series["due"]),
        }
        nxt = (datetime.fromisoformat(date) + timedelta(days=1)).date().isoformat()
        series["due"] = series["due"] | {"date": nxt}
        return copy.deepcopy(series)

    def log_habit(self, habit_id: str, day: str, state: str, timezone: str | None) -> JSON:
        """Like OpenTodo: the current day completes and moves the habit; an earlier day from
        the habit's first one gets its own record; a later day is refused."""
        self.writes.append(("log_habit", (habit_id, day, state)))
        habit = self.tasks_[habit_id]
        due = habit["due"]["date"]
        records = {
            t["occurrence_date"]: t
            for t in self.tasks_.values()
            if t.get("series_id") == habit_id and t["deleted_at"] is None
        }
        first = min([habit.setdefault("_first", due), *records])
        if state == "clear":
            if day in records:
                records[day]["deleted_at"] = 1
        elif day > due or day < first:
            raise ApiError("OpenTodo", "POST", "log", 400, "not a scheduled day")
        else:
            rid = self.add_task(habit["title"], habit["project_id"])
            self.tasks_[rid] |= {
                "series_id": habit_id,
                "occurrence_date": day,
                "completed_at": 1,
                "skipped": state == "skipped",
            }
            if day == due:
                # OpenTodo moves to the next day that isn't past: an overdue habit jumps past
                # today (seen 2026-11-24), whatever day was completed.
                nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
                if self.today and nxt <= self.today:
                    nxt = (
                        (datetime.fromisoformat(self.today) + timedelta(days=1)).date().isoformat()
                    )
                habit["due"] = {"date": nxt}
        return {"habit": {"id": habit_id, "current_date": habit["due"]["date"]}}

    def delete_task(self, task_id: str) -> None:
        self.writes.append(("delete", task_id))
        self.tasks_[task_id]["deleted_at"] = 1
