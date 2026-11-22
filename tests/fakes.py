"""In-memory stand-ins for both APIs, faithful to the behaviour in docs/api-notes.md."""

from __future__ import annotations

import copy
import itertools
from datetime import timedelta
from typing import Any

from ticktick_opentodo_sync.mapping import format_tt_datetime, parse_tt_datetime
from ticktick_opentodo_sync.models import JSON

_ids = itertools.count(1)


def _id(prefix: str) -> str:
    return f"{prefix}{next(_ids):04d}"


class FakeTickTick:
    def __init__(self, tz: str = "America/New_York") -> None:
        self.tz = tz
        self.inbox_id = "inbox100"
        self.projects_: dict[str, JSON] = {}
        self.tasks: dict[str, JSON] = {}
        self.writes: list[tuple[str, Any]] = []

    # helpers for tests
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

    def get_task(self, project_id: str, task_id: str) -> JSON | None:
        t = self.tasks.get(task_id)
        return copy.deepcopy(t) if t else None

    def create_task(self, body: JSON) -> JSON:
        self.writes.append(("create", body))
        tid = _id("tt")
        pid = self.inbox_id if body["projectId"] == "inbox" else body["projectId"]
        self.tasks[tid] = {"status": 0, "kind": "TEXT"} | body | {"id": tid, "projectId": pid}
        return copy.deepcopy(self.tasks[tid])

    def update_task(self, task_id: str, body: JSON) -> JSON:
        self.writes.append(("update", body))
        self.tasks[task_id] = self.tasks[task_id] | body
        return copy.deepcopy(self.tasks[task_id])

    def complete_task(self, project_id: str, task_id: str) -> None:
        self.writes.append(("complete", task_id))
        t = self.tasks[task_id]
        if t.get("repeatFlag") and t.get("dueDate"):
            # Recurring: the task stays open and advances; a completed copy is split off.
            done = copy.deepcopy(t) | {"id": _id("tt"), "status": 2}
            self.tasks[done["id"]] = done
            nxt = parse_tt_datetime(t["dueDate"]) + timedelta(days=1)
            t["dueDate"] = t["startDate"] = format_tt_datetime(nxt)
        else:
            t["status"] = 2

    def delete_task(self, project_id: str, task_id: str) -> None:
        self.writes.append(("delete", task_id))
        self.tasks.pop(task_id, None)

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

    def delete_task(self, task_id: str) -> None:
        self.writes.append(("delete", task_id))
        self.tasks_[task_id]["deleted_at"] = 1
