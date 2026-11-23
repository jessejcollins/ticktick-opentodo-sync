"""The normalized task shape both sides are converted to and compared in."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

INBOX = "inbox"
"""Project key of the Inbox on both sides."""

TICKTICK_ONLY_PREFIX = "ticktick-only:"
"""``Task.recurrence`` prefix for a TickTick rule OpenTodo can't express (kept, never copied)."""

PENDING_PREFIX = "ot:"
"""Project key prefix for an OpenTodo project not yet created in TickTick (dry run only)."""

JSON = dict[str, Any]


class Priority(IntEnum):
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True, slots=True)
class Due:
    """A due date; ``time`` ("HH:MM") and ``tz`` (IANA) are set together or not at all."""

    date: str
    time: str | None = None
    tz: str | None = None

    def to_json(self) -> JSON:
        out: JSON = {"date": self.date}
        if self.time is not None:
            out["time"] = self.time
            out["tz"] = self.tz
        return out

    @classmethod
    def from_json(cls, data: JSON) -> Due:
        return cls(date=data["date"], time=data.get("time"), tz=data.get("tz"))


@dataclass(frozen=True, slots=True)
class Task:
    """One task, independent of either API.

    ``project`` is a project *key*: :data:`INBOX`, a TickTick project id, or
    ``ot:<id>`` for an OpenTodo project that has no TickTick twin yet.
    """

    title: str
    project: str = INBOX
    notes: str = ""
    due: Due | None = None
    priority: Priority = Priority.NONE
    tags: frozenset[str] = field(default_factory=frozenset)
    completed: bool = False
    recurrence: str | None = None
    """OpenTodo rule (canonical, e.g. ``every monday``), ``ticktick-only:<RRULE>``, or None."""
    parent: str | None = None
    """Subtask: TickTick id of the parent task (``ot:<id>`` while it has no TickTick twin)."""
    reminders: frozenset[int] = field(default_factory=frozenset)
    """Reminder offsets in minutes, OpenTodo's way: from the due time, or from 09:00 on the
    day for an all-day task; negative = before."""

    def to_json(self) -> JSON:
        out: JSON = {
            "title": self.title,
            "project": self.project,
            "notes": self.notes,
            "due": self.due.to_json() if self.due else None,
            "priority": int(self.priority),
            "tags": sorted(self.tags),
            "completed": self.completed,
        }
        if self.recurrence is not None:
            # Only when set: tasks without a rule keep the hashes stored before phase 2.
            out["recurrence"] = self.recurrence
        if self.parent is not None:
            out["parent"] = self.parent
        if self.reminders:
            out["reminders"] = sorted(self.reminders)
        return out

    @property
    def ticktick_only_rule(self) -> bool:
        return (self.recurrence or "").startswith(TICKTICK_ONLY_PREFIX)

    @classmethod
    def from_json(cls, data: JSON) -> Task:
        due = data.get("due")
        return cls(
            title=data["title"],
            project=data["project"],
            notes=data.get("notes", ""),
            due=Due.from_json(due) if due else None,
            priority=Priority(data.get("priority", 0)),
            tags=frozenset(data.get("tags", ())),
            completed=bool(data.get("completed", False)),
            recurrence=data.get("recurrence"),
            parent=data.get("parent"),
            reminders=frozenset(data.get("reminders", ())),
        )

    def content_hash(self) -> str:
        blob = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def same_content(self, other: Task) -> bool:
        """Equal in everything except completion."""
        return self.to_json() | {"completed": None} == other.to_json() | {"completed": None}


def changed_fields(old: Task, new: Task) -> list[str]:
    """Names of the :class:`Task` fields that differ (for logs)."""
    a, b = old.to_json(), new.to_json()
    return [k for k in a | b if a.get(k) != b.get(k)]
