"""TickTick Open API client (personal API token). Endpoints: docs/api-notes.md."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from .e2ee import is_ciphertext
from .http import Http
from .mapping import format_tt_datetime
from .models import JSON
from .opentodo import PLACEHOLDER

BASE_URL = "https://api.ticktick.com"
PROJECT_PAGE = 200
"""Lists per ``GET /open/v1/project`` page (the documented default ``limit``)."""
MAX_PROJECT_PAGES = 50


class CiphertextWriteError(ValueError):
    """A write would put OpenTodo ciphertext into TickTick (the 2026-11-21 incident)."""


def refuse_ciphertext(body: object, path: str = "body") -> None:
    """Raise if any string in ``body`` is OpenTodo ciphertext or the sync's create placeholder."""
    if is_ciphertext(body):
        raise CiphertextWriteError(f"refusing to write OpenTodo ciphertext to TickTick ({path})")
    if body == PLACEHOLDER:
        raise CiphertextWriteError(f"refusing to write the create placeholder to TickTick ({path})")
    if isinstance(body, dict):
        for k, v in body.items():
            refuse_ciphertext(v, f"{path}.{k}")
    elif isinstance(body, list):
        for i, v in enumerate(body):
            refuse_ciphertext(v, f"{path}[{i}]")


class TickTick:
    def __init__(self, http: Http) -> None:
        self.http = http

    def preference_timezone(self) -> str | None:
        prefs = self.http.request("POST", "/open/v1/preference", idempotent=True)  # a read
        tz = prefs.get("timeZone") if isinstance(prefs, dict) else None
        return tz if isinstance(tz, str) and tz else None

    def projects(self) -> list[JSON]:
        """Every list, a page at a time (``offset``/``limit``, documented default 200).

        A list missing from the answer reads as deleted, and its OpenTodo project is then
        archived, so a page that can't be continued raises rather than stopping short.
        """
        seen: dict[str, JSON] = {}
        offset = 0
        for _ in range(MAX_PROJECT_PAGES):
            params = {"offset": str(offset), "limit": str(PROJECT_PAGE)}
            page = _list(self.http.request("GET", "/open/v1/project", params=params))
            new = {str(p["id"]): p for p in page if p.get("id") and str(p["id"]) not in seen}
            if len(page) == PROJECT_PAGE and not new:
                raise TypeError("TickTick ignored the list offset: some lists may be missing")
            seen |= new
            offset += len(page)
            if len(page) != PROJECT_PAGE:  # the last page, or limit ignored: everything at once
                return list(seen.values())
        raise TypeError(f"more than {MAX_PROJECT_PAGES * PROJECT_PAGE} TickTick lists")

    def project_data(self, project_id: str) -> JSON:
        """``{project, tasks, columns}`` (the undone tasks). Anything else raises: read as
        "no tasks", an odd answer made every paired task in the list look deleted."""
        data = self.http.request("GET", f"/open/v1/project/{project_id}/data")
        if isinstance(data, dict):
            tasks = data.get("tasks")
            if isinstance(tasks, list):
                return cast(JSON, data)
            # A list without tasks may come without the field (UNVERIFIED); an answer with
            # none of the fields (an error object, {}) is no answer at all.
            if tasks is None and ("project" in data or "columns" in data):
                return cast(JSON, data)
        raise TypeError(f"unexpected TickTick response for list {project_id}: {_shape(data)}")

    def get_task(self, project_id: str, task_id: str) -> JSON | None:
        """The task, or None when it's gone: a 404, or an empty answer.

        UNVERIFIED whether TickTick answers a deleted task with an empty 200 rather than a
        404 (docs/api-notes.md), so that still reads as gone. Any other answer that isn't
        this task raises instead of reading as "deleted".
        """
        data = self.http.request(
            "GET", f"/open/v1/project/{project_id}/task/{task_id}", not_found_ok=True
        )
        if data is None or data == {}:
            return None
        if isinstance(data, dict) and str(data.get("id")) == task_id:
            return cast(JSON, data)
        raise TypeError(f"unexpected TickTick response for task {task_id}: {_shape(data)}")

    def create_task(self, body: JSON) -> JSON:
        refuse_ciphertext(body)
        return _obj(self.http.request("POST", "/open/v1/task", json=body))

    def update_task(self, task_id: str, body: JSON) -> JSON:
        refuse_ciphertext(body)
        # The whole task: sending it twice gives the same task.
        return _obj(
            self.http.request(
                "POST", f"/open/v1/task/{task_id}", json=body | {"id": task_id}, idempotent=True
            )
        )

    def completed_tasks(self, project_ids: list[str], start: datetime, end: datetime) -> list[JSON]:
        """Tasks completed between ``start`` and ``end`` (by completion time; at most 200)."""
        body = {
            "projectIds": project_ids,
            "startDate": format_tt_datetime(start),
            "endDate": format_tt_datetime(end),
        }
        return _list(
            self.http.request("POST", "/open/v1/task/completed", json=body, idempotent=True)
        )

    def complete_task(self, project_id: str, task_id: str) -> None:
        # Not repeatable: a repeating task would move on twice, skipping an occurrence.
        self.http.request("POST", f"/open/v1/project/{project_id}/task/{task_id}/complete")

    def delete_task(self, project_id: str, task_id: str) -> None:
        self.http.request(
            "DELETE", f"/open/v1/project/{project_id}/task/{task_id}", not_found_ok=True
        )

    def move_task(self, task_id: str, from_project: str, to_project: str) -> None:
        body = [{"fromProjectId": from_project, "toProjectId": to_project, "taskId": task_id}]
        self.http.request("POST", "/open/v1/task/move", json=body, idempotent=True)

    # habits ---------------------------------------------------------------------------

    def habits(self) -> list[JSON]:
        return _list(self.http.request("GET", "/open/v1/habit"))

    def habit_sections(self) -> list[JSON]:
        return _list(self.http.request("GET", "/open/v1/habit/sections"))

    def habit_checkins(self, habit_ids: list[str], from_stamp: int, to_stamp: int) -> list[JSON]:
        """Check-in documents (``{habitId, checkins: [{stamp, status, ...}]}``).

        Both stamps (``YYYYMMDD``) are inclusive here; TickTick's ``to`` is exclusive.
        """
        if not habit_ids:
            return []
        end = datetime.strptime(str(to_stamp), "%Y%m%d").date() + timedelta(days=1)
        params = {
            "habitIds": ",".join(habit_ids),
            "from": str(from_stamp),
            "to": end.strftime("%Y%m%d"),
        }
        return _list(self.http.request("GET", "/open/v1/habit/checkins", params=params))

    def create_habit(self, body: JSON) -> JSON:
        refuse_ciphertext(body)
        return _obj(self.http.request("POST", "/open/v1/habit", json=body))

    def update_habit(self, habit_id: str, body: JSON) -> JSON:
        refuse_ciphertext(body)
        return _obj(
            self.http.request("POST", f"/open/v1/habit/{habit_id}", json=body, idempotent=True)
        )

    def checkin(self, habit_id: str, body: JSON) -> JSON:
        """Create or update the check-in of ``body["stamp"]`` (YYYYMMDD): repeatable."""
        return _obj(
            self.http.request(
                "POST", f"/open/v1/habit/{habit_id}/checkin", json=body, idempotent=True
            )
        )

    def create_project(self, name: str) -> JSON:
        body = {"name": name, "viewMode": "list", "kind": "TASK"}
        refuse_ciphertext(body)
        return _obj(self.http.request("POST", "/open/v1/project", json=body))


def _list(data: Any) -> list[JSON]:
    """A list response; anything else raises (never read as "none")."""
    if not isinstance(data, list):
        raise TypeError(f"unexpected TickTick response: {type(data).__name__}, not a list")
    return [x for x in data if isinstance(x, dict)]


def _obj(data: Any) -> JSON:
    return cast(JSON, data) if isinstance(data, dict) else {}


def _shape(data: Any) -> str:
    """A response's shape for an error message, without its content."""
    if isinstance(data, dict):
        return f"an object with keys {sorted(map(str, data))[:8]}"
    return "an empty answer" if data is None else type(data).__name__
