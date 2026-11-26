"""One sync pass: fetch both sides, reconcile projects, plan, guard, apply, record."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from . import engine, recurrence
from .engine import Action, Kind, Pair, Snapshot
from .habit_sync import HABITS_PROJECT, META_PROJECT, HabitSync
from .http import ApiError
from .mapping import (
    TT_DONE,
    TT_DROP_ON_MERGE,
    TT_ITEM_DONE,
    Mapper,
    ProjectInfo,
    ProjectPlan,
    due_from_tt,
    item_from_ot,
    item_from_tt,
    norm_title,
    parse_tt_datetime,
    reconcile_projects,
)
from .models import INBOX, JSON, PENDING_PREFIX, TICKTICK_ONLY_PREFIX, Task, changed_fields
from .opentodo import PLACEHOLDER, UNREADABLE
from .state import ITEMS, State

log = logging.getLogger(__name__)


class TickTickApi(Protocol):
    def preference_timezone(self) -> str | None: ...
    def projects(self) -> list[JSON]: ...
    def project_data(self, project_id: str) -> JSON: ...
    def get_task(self, project_id: str, task_id: str) -> JSON | None: ...
    def create_task(self, body: JSON) -> JSON: ...
    def update_task(self, task_id: str, body: JSON) -> JSON: ...
    def complete_task(self, project_id: str, task_id: str) -> None: ...
    def completed_tasks(
        self, project_ids: list[str], start: datetime, end: datetime
    ) -> list[JSON]: ...
    def delete_task(self, project_id: str, task_id: str) -> None: ...
    def move_task(self, task_id: str, from_project: str, to_project: str) -> None: ...
    def create_project(self, name: str) -> JSON: ...
    def habits(self) -> list[JSON]: ...
    def habit_sections(self) -> list[JSON]: ...
    def habit_checkins(
        self, habit_ids: list[str], from_stamp: int, to_stamp: int
    ) -> list[JSON]: ...
    def create_habit(self, body: JSON) -> JSON: ...
    def update_habit(self, habit_id: str, body: JSON) -> JSON: ...
    def checkin(self, habit_id: str, body: JSON) -> JSON: ...


class OpenTodoApi(Protocol):
    def begin_pass(self) -> None: ...
    def projects(self) -> list[JSON]: ...
    def project(self, project_id: str) -> JSON | None: ...
    def create_project(self, name: str) -> JSON: ...
    def rename_project(self, project_id: str, name: str) -> JSON: ...
    def archive_project(self, project_id: str) -> JSON: ...
    def restore_project(self, project_id: str) -> JSON: ...
    def labels(self) -> list[JSON]: ...
    def create_label(self, name: str) -> JSON: ...
    def tasks(self) -> list[JSON]: ...
    def create_task(self, body: JSON, *, idempotency_key: str) -> JSON: ...
    def update_task(self, task_id: str, body: JSON) -> JSON: ...
    def delete_task(self, task_id: str) -> None: ...
    def complete_occurrence(self, task_id: str, date: str, completed_at: int | None) -> JSON: ...
    def log_habit(self, habit_id: str, day: str, state: str, timezone: str | None) -> JSON: ...


@dataclass
class SideCounts:
    created: int = 0
    updated: int = 0
    completed: int = 0
    deleted: int = 0

    def __str__(self) -> str:
        return (
            f"created={self.created} updated={self.updated} "
            f"completed={self.completed} deleted={self.deleted}"
        )


@dataclass
class PassResult:
    dry_run: bool
    tt: SideCounts = field(default_factory=SideCounts)
    ot: SideCounts = field(default_factory=SideCounts)
    tt_items: SideCounts = field(default_factory=SideCounts)
    ot_items: SideCounts = field(default_factory=SideCounts)
    """Checklist items (TickTick) / their subtasks (OpenTodo)."""
    habits_tt: SideCounts = field(default_factory=SideCounts)
    habits_ot: SideCounts = field(default_factory=SideCounts)
    """Habits: created/updated definitions, check-ins added (completed) and cleared (deleted)."""
    conflicts: int = 0
    errors: int = 0
    ot_tasks_read: int = 0
    """Tasks OpenTodo sent (open, completed, trashed, habit days): a count that only grows,
    all of it held in memory each pass (the container has 128 MB)."""
    blocked: str | None = None
    """A bulk guard tripped and what it guards wasn't written: nothing at all for the task
    guards, the habit check-ins for the habit guard (which runs after the task pass)."""
    refused: str | None = None
    """First-sync guard tripped; nothing was written."""

    @property
    def changed(self) -> bool:
        sides = (self.tt, self.ot, self.tt_items, self.ot_items, self.habits_tt, self.habits_ot)
        return any(any(asdict(c).values()) for c in sides)

    @property
    def ok(self) -> bool:
        return not (self.errors or self.blocked or self.refused)

    def summary(self) -> str:
        prefix = "DRY-RUN " if self.dry_run else ""
        line = (
            f"{prefix}TickTick: {self.tt} | OpenTodo: {self.ot} | "
            f"items TickTick: {self.tt_items} | items OpenTodo: {self.ot_items} | "
            f"habits TickTick: {self.habits_tt} | habits OpenTodo: {self.habits_ot} | "
            f"conflicts={self.conflicts} errors={self.errors} | "
            f"OpenTodo tasks read={self.ot_tasks_read}"
        )
        if self.blocked:
            line += " | BLOCKED"
        if self.refused:
            line += " | REFUSED"
        return line

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


@dataclass
class World:
    """Everything one pass knows after fetching."""

    mapper: Mapper
    tt: Snapshot
    ot: Snapshot
    pairs: dict[str, Pair]
    tt_raw: dict[str, JSON] = field(default_factory=dict)
    ot_raw: dict[str, JSON] = field(default_factory=dict)
    label_ids: dict[str, str] = field(default_factory=dict)
    """Lowercased label name -> OpenTodo label id."""
    project_names: dict[str, str] = field(default_factory=dict)
    """Project key -> display name (for logs)."""
    item_pairs: dict[str, Pair] = field(default_factory=dict)
    tt_items: Snapshot = field(default_factory=Snapshot)
    ot_items: Snapshot = field(default_factory=Snapshot)
    tt_item_raw: dict[str, JSON] = field(default_factory=dict)
    ot_habits: dict[str, JSON] = field(default_factory=dict)
    """Open OpenTodo habits (the habit task itself, not its day records)."""
    ot_records: list[JSON] = field(default_factory=list)
    """Every live OpenTodo occurrence record (``series_id`` set)."""
    ot_project_ids: set[str] = field(default_factory=set)
    projects: ProjectPlan = field(default_factory=ProjectPlan)
    """List/project changes: planned by :meth:`Syncer.collect`, applied after the guards."""
    project_renames: list[tuple[str, str]] = field(default_factory=list)
    """(OpenTodo project id, name) of paired projects holding the create placeholder."""
    retiring: set[str] = field(default_factory=set)
    """Project keys of OpenTodo projects whose TickTick list was deleted: only their paired
    tasks are in scope (for the delete rules); new ones are never copied to TickTick."""
    tt_lists_alive: set[str] = field(default_factory=set)
    """TickTick lists a looked-up task was found in: they exist, listed or not."""
    ot_tasks_read: int = 0


class SyncRefusedError(Exception):
    pass


class ChangedDuringPassError(Exception):
    """The target changed after the pass read it: writing now would undo that change."""


class Syncer:
    def __init__(
        self,
        tt: TickTickApi,
        ot: OpenTodoApi,
        state: State,
        *,
        timezone: str | None = None,
        max_destructive: int = 10,
        max_deletes_per_day: int = 50,
        max_creates: int = 50,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.tt = tt
        self.ot = ot
        self.state = state
        self.timezone = timezone
        self.max_destructive = max_destructive
        self.max_deletes_per_day = max_deletes_per_day
        self.max_creates = max_creates
        self.now_ms = now_ms
        self.run_id = str(now_ms())
        self._habit_notes: set[str] = set()
        """The habit sync's once-per-process warnings: it's rebuilt every pass, this isn't."""

    # --- fetch ------------------------------------------------------------------------

    def collect(self, *, dry_run: bool) -> World:
        self.run_id = str(self.now_ms())
        self.ot.begin_pass()
        tz = self.timezone or self.tt.preference_timezone() or "UTC"
        mapper = Mapper(default_tz=tz, tt_inbox_id=self.state.get_meta("tt_inbox_id"))

        tt_projects = {
            str(p["id"]): ProjectInfo(str(p.get("name") or ""), not p.get("closed"))
            for p in self.tt.projects()
            if p.get("id") and str(p.get("kind") or "TASK").upper() != "NOTE"
        }
        ot_projects: dict[str, ProjectInfo] = {}
        ot_project_ids: set[str] = set()
        trashed: set[str] = set()
        renames: list[tuple[str, str]] = []
        ot_project_list = self._with_unlisted_projects(self.ot.projects())
        ot_task_list = self.ot.tasks()  # before reconciling projects: see _find_habits_project
        habits_project = self.state.get_meta(META_PROJECT) or self._find_habits_project(
            ot_project_list, ot_task_list, dry_run=dry_run
        )
        names = {
            ot: tt_projects[tt].name
            for tt, ot in self.state.project_pairs().items()
            if tt in tt_projects
        }
        if habits_project:
            names[habits_project] = HABITS_PROJECT
        for p in ot_project_list:
            if not p.get("id"):
                continue
            pid = str(p["id"])
            if p.get("deleted_at") is not None:
                trashed.add(pid)
                continue
            # A paired project whose name an open browser overwrote with the create
            # placeholder is renamed back once the guards pass; unreadable until then.
            repair = names.get(pid) if p.get("name") == PLACEHOLDER else None
            if repair and not p.get(UNREADABLE):
                renames.append((pid, repair))
            ot_project_ids.add(pid)
            if pid == habits_project:
                continue  # the habit sync's own project: never a TickTick list
            if _unreadable(p, "name"):
                # Kept (so a stored pair survives) but inactive: its tasks stay out of scope
                # and its name is never matched or copied.
                if not repair:
                    log.warning("OpenTodo project %s: name unreadable; leaving it alone", pid)
                ot_projects[pid] = ProjectInfo(f"<unreadable {pid}>", False)
            else:
                ot_projects[pid] = ProjectInfo(str(p.get("name") or ""), not p.get("archived"))
        plan = reconcile_projects(self.state.project_pairs(), tt_projects, ot_projects, trashed)
        project_map = dict(plan.pairs)
        mapper.tt_to_ot_project = project_map

        in_scope = {INBOX}
        names = {INBOX: "Inbox"}
        for tt_id, info in tt_projects.items():
            names[tt_id] = info.name
            ot_id = project_map.get(tt_id)
            ot_info = ot_projects.get(ot_id) if ot_id else None
            if info.active and (ot_info is None or ot_info.active):
                in_scope.add(tt_id)
        paired_ot = set(project_map.values())
        for ot_id, info in ot_projects.items():
            if ot_id not in paired_ot:
                names[PENDING_PREFIX + ot_id] = info.name
                if info.active:
                    in_scope.add(PENDING_PREFIX + ot_id)

        world = World(
            mapper=mapper,
            tt=Snapshot(),
            ot=Snapshot(),
            pairs={p.tt_id: p for p in self.state.pairs()},
            project_names=names,
            item_pairs={p.tt_id: p for p in self.state.pairs(ITEMS)},
            ot_project_ids=ot_project_ids,
            projects=plan,
            project_renames=renames,
            retiring={PENDING_PREFIX + ot_id for _, ot_id in plan.retire},
            ot_tasks_read=len(ot_task_list),
        )
        for pair in world.pairs.values():
            mapper.link_task(pair.tt_id, pair.ot_id)
        self._collect_tt(world, [pid for pid in tt_projects if pid in in_scope], in_scope, dry_run)
        children = self._collect_ot(world, in_scope, ot_task_list)
        self._resolve_missing_tt(world, in_scope)
        self._refresh_tt_projects(world, dry_run=dry_run)
        self._collect_subtasks(world, children, in_scope)
        self._hold_retiring(world)
        return world

    def _with_unlisted_projects(self, listed: list[JSON]) -> list[JSON]:
        """OpenTodo's project listing, plus paired projects it left out that still exist.

        A paired project missing from one listing read as purged: the pair was dropped and
        the list got a new project, and once the old one was listed again it became a
        second TickTick list, taking the original list's tasks with it. So each one is
        looked up: a 404 means gone; a project that answers (trashed or not) is used.
        """
        listed_ids = {str(p.get("id")) for p in listed}
        out = list(listed)
        for pid in sorted(set(self.state.project_pairs().values()) - listed_ids):
            found = self.ot.project(pid)
            if found is not None:
                log.warning(
                    "OpenTodo project %s is missing from the project listing but still exists; "
                    "using it",
                    pid,
                )
                out.append(found)
        return out

    def _hold_retiring(self, w: World) -> None:
        """Archive a project only once its TickTick list is really gone.

        A list missing from one listing read as deleted: its project was archived, and the
        list's tasks, still paired with copies in the archived project, stopped syncing for
        good. A task of that list found alive under it (see :meth:`_resolve_missing_tt`)
        proves the list still exists, so its project is kept this pass.
        """
        held = [r for r in w.projects.retire if r[0] in w.tt_lists_alive]
        for tt_id, ot_id in held:
            log.warning(
                "TickTick list %s is missing from the list listing but its tasks are still in "
                "it; not archiving OpenTodo project %r",
                tt_id,
                w.project_names.get(PENDING_PREFIX + ot_id, ot_id),
            )
        w.projects.retire = [r for r in w.projects.retire if r not in held]

    def _refresh_tt_projects(self, w: World, *, dry_run: bool) -> None:
        """Keep each pair's TickTick list id on the list the task was last seen in.

        GETs and DELETEs of a TickTick task go to its list. A task moved between lists in
        TickTick and later completed was looked up under its old list, which may 404 and
        read as "deleted": its OpenTodo copy went to the Trash instead of being completed.
        Written only when it changed.
        """
        for tt_id, p in list(w.pairs.items()):
            seen = (w.tt_raw.get(tt_id) or {}).get("projectId")
            if not seen or str(seen) == p.tt_project:
                continue
            w.pairs[tt_id] = replace(p, tt_project=str(seen))
            if not dry_run:
                self.state.update_pair(tt_id, tt_project=str(seen))

    # --- lists and projects -------------------------------------------------------------

    def _find_habits_project(
        self, projects: list[JSON], tasks: list[JSON], *, dry_run: bool
    ) -> str | None:
        """The habit sync's OpenTodo project when its id isn't in the state (after a reset).

        Without it, the old "Habits" project read as an ordinary project and was turned into
        a TickTick list. Taken back: a live project named "Habits" that holds open habits.
        """
        holding = {
            str(t["project_id"])
            for t in tasks
            if t.get("habit")
            and t.get("project_id")
            and not t.get("series_id")
            and t.get("deleted_at") is None
        }
        for p in projects:
            pid = str(p.get("id") or "")
            if (
                pid in holding
                and p.get("deleted_at") is None
                and str(p.get("name") or "").strip().casefold() == HABITS_PROJECT.casefold()
            ):
                log.info(
                    "%suse OpenTodo project %r (%s) for habits again",
                    "would " if dry_run else "",
                    HABITS_PROJECT,
                    pid,
                )
                if not dry_run:
                    self.state.set_meta(META_PROJECT, pid)
                return pid
        return None

    def _apply_projects(self, w: World, *, dry_run: bool) -> bool:
        """Apply the list/project changes :meth:`collect` planned (the guards have passed).

        Returns True when lists or projects were created, renamed or restored: tasks map to
        them only in a fresh collect. Archiving waits for the end of the pass
        (:meth:`_retire_projects`).
        """
        plan, verb = w.projects, "would " if dry_run else ""
        remote = bool(w.project_renames or plan.restore or plan.create_in_ot or plan.create_in_tt)
        if remote and not dry_run:
            self.state.check_writable()
        for ot_id, name in w.project_renames:
            log.info(
                "%srename OpenTodo project %s back to %r (it held the create placeholder)",
                verb,
                ot_id,
                name,
            )
            if not dry_run:
                self.ot.rename_project(ot_id, name)
        for tt_id, ot_id in plan.restore:
            name = w.project_names.get(tt_id, tt_id)
            log.info("%srestore OpenTodo project %r from the Trash (TickTick wins)", verb, name)
            if not dry_run:
                self.ot.restore_project(ot_id)
        for tt_id in plan.stale:
            log.info("%sforget the project pair of TickTick list %s", verb, tt_id)
            if not dry_run:
                self.state.drop_project_pair(tt_id)
        for tt_id, ot_id, name in plan.new_pairs:
            log.info("%spair project %r by name", verb, name)
            if not dry_run:
                self.state.add_project_pair(tt_id, ot_id, name)
        for tt_id, name in plan.create_in_ot:
            log.info("%screate OpenTodo project %r", verb, name)
            if not dry_run:
                created = self.ot.create_project(name)
                self.state.add_project_pair(tt_id, str(created["id"]), name)
        for ot_id, name in plan.create_in_tt:
            log.info("%screate TickTick list %r", verb, name)
            if not dry_run:
                created = self.tt.create_project(name)
                self.state.add_project_pair(str(created["id"]), ot_id, name)
        if dry_run:
            for _, ot_id in plan.retire:
                name = w.project_names.get(PENDING_PREFIX + ot_id, ot_id)
                log.info("would archive OpenTodo project %r: its TickTick list was deleted", name)
        return remote and not dry_run

    def _retire_projects(self, w: World, res: PassResult) -> None:
        """Archive the OpenTodo projects whose TickTick list was deleted; forget the pairs.

        Only after a pass without errors: their paired tasks must have gone through the
        delete rules first, as an archived project's tasks are out of scope for good.
        """
        if not w.projects.retire:
            return
        if res.errors:
            log.warning(
                "archiving the OpenTodo project(s) of deleted TickTick lists waits for a pass "
                "without errors"
            )
            return
        self.state.check_writable()
        for tt_id, ot_id in w.projects.retire:
            name = w.project_names.get(PENDING_PREFIX + ot_id, ot_id)
            try:
                self.ot.archive_project(ot_id)
            except Exception as exc:
                # Counted, and tried again next pass. Raising skipped the rest of the pass:
                # its deletions went unrecorded (daily budget) and habits didn't sync.
                res.errors += 1
                log.error("failed to archive OpenTodo project %r: %s", name, exc)  # noqa: TRY400
                log.debug("traceback", exc_info=True)
                continue
            self.state.drop_project_pair(tt_id)
            log.info("archived OpenTodo project %r: its TickTick list was deleted", name)

    def _collect_tt(
        self, w: World, project_ids: list[str], in_scope: set[str], dry_run: bool
    ) -> None:
        for pid in [*project_ids, INBOX]:
            try:
                data = self.tt.project_data(pid)
            except ApiError as exc:
                if pid != INBOX or exc.status != 404:
                    raise
                # UNVERIFIED endpoint (docs/api-notes.md): degrade instead of failing the pass.
                log.warning("TickTick inbox not readable via /project/inbox/data: %s", exc)
                continue
            tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict) and t.get("id")]
            if pid == INBOX:
                inbox_id = (data.get("project") or {}).get("id") or next(
                    (t.get("projectId") for t in tasks if t.get("projectId")), None
                )
                if inbox_id and inbox_id != w.mapper.tt_inbox_id:
                    log.info("TickTick inbox id is %s", inbox_id)
                    w.mapper.tt_inbox_id = str(inbox_id)
                    if not dry_run:
                        self.state.set_meta("tt_inbox_id", str(inbox_id))
            for raw in tasks:
                w.tt_raw[str(raw["id"])] = raw

        # Top-level tasks first, then subtasks of in-scope top-level tasks (one level deep).
        for tid, raw in sorted(w.tt_raw.items(), key=lambda kv: bool(kv[1].get("parentId"))):
            task = self._normalize(w.mapper.from_tt, raw, "TickTick", tid)
            parent = raw.get("parentId")
            if (
                task is None
                or task.project not in in_scope
                or (parent and not self._top_level_tt(w, str(parent)))
            ):
                w.tt.out_of_scope.add(tid)
            else:
                w.tt.tasks[tid] = task

    @staticmethod
    def _top_level_tt(w: World, tt_id: str) -> bool:
        return tt_id in w.tt.tasks and not w.tt_raw.get(tt_id, {}).get("parentId")

    def _collect_ot(self, w: World, in_scope: set[str], ot_tasks: list[JSON]) -> list[JSON]:
        """Top-level OpenTodo tasks; returns the open subtasks for :meth:`_collect_subtasks`."""
        unreadable_labels: set[str] = set()
        for label in self.ot.labels():
            if label.get("deleted_at") is not None or not label.get("id"):
                continue
            if _unreadable(label, "name"):
                log.warning(
                    "OpenTodo label %s: name unreadable; its tasks are skipped", label["id"]
                )
                unreadable_labels.add(str(label["id"]))
            elif label.get("name"):
                name = str(label["name"]).strip().lower()
                w.mapper.label_names[str(label["id"])] = name
                w.label_ids.setdefault(name, str(label["id"]))

        children: list[JSON] = []
        paired = {p.ot_id for p in w.pairs.values()}
        for raw in ot_tasks:
            oid = str(raw.get("id") or "")
            if not oid:
                continue
            if raw.get("deleted_at") is not None:
                w.ot.deleted.add(oid)
                w.ot_items.deleted.add(oid)
                continue
            if raw.get("series_id") and raw.get("completed_at") and not raw.get("skipped"):
                occ = raw.get("occurrence_date")
                if occ:
                    w.ot.completed_occurrences.setdefault(str(raw["series_id"]), set()).add(
                        str(occ)
                    )
            if raw.get("series_id"):
                w.ot_records.append(raw)
            elif raw.get("habit") and raw.get("completed_at") is None and not raw.get(UNREADABLE):
                w.ot_habits[oid] = raw
            if raw.get("series_id") or raw.get("habit"):
                w.ot.out_of_scope.add(oid)
                continue
            if raw.get("parent_id"):
                children.append(raw)
                continue
            if not _damaged(w.ot, raw, paired):
                w.ot.out_of_scope.add(oid)
                continue
            if raw.get(UNREADABLE) or unreadable_labels.intersection(raw.get("labels") or []):
                # Never let unreadable content look like an edit (or a deletion).
                log.warning("OpenTodo task %s: content or a label unreadable; skipping it", oid)
                w.ot.out_of_scope.add(oid)
                continue
            w.ot_raw[oid] = raw
            task = self._normalize(w.mapper.from_ot, raw, "OpenTodo", oid)
            if (
                task is None
                or task.project not in in_scope
                or (task.project in w.retiring and oid not in paired)
            ):
                w.ot.out_of_scope.add(oid)
            else:
                w.ot.tasks[oid] = task
        return children

    def _items_in_scope(self, w: World, tt_parent: str) -> bool:
        """Checklist items sync only while the parent is open, paired and in scope on both sides."""
        tt_task = w.tt.tasks.get(tt_parent)
        pair = w.pairs.get(tt_parent)
        ot_task = w.ot.tasks.get(pair.ot_id) if pair else None
        return (
            tt_task is not None
            and ot_task is not None
            and not tt_task.completed
            and not ot_task.completed
            and not w.tt_raw.get(tt_parent, {}).get("parentId")
        )

    def _collect_subtasks(self, w: World, children: list[JSON], in_scope: set[str]) -> None:
        """OpenTodo subtasks become items or subtasks; TickTick checklist items are read."""
        item_ot_ids = {p.ot_id: p for p in w.item_pairs.values()}
        task_ot_ids = {p.ot_id for p in w.pairs.values()}
        for raw in children:
            oid, parent_ot = str(raw["id"]), str(raw["parent_id"])
            snap = w.ot_items if oid in item_ot_ids else w.ot
            if not _damaged(snap, raw, item_ot_ids.keys() | task_ot_ids):
                w.ot.out_of_scope.add(oid)
                w.ot_items.out_of_scope.add(oid)
                continue
            parent_tt = w.mapper.ot_to_tt_task.get(parent_ot)
            parent_raw = w.ot_raw.get(parent_ot)
            kind = (w.tt_raw.get(parent_tt or "") or {}).get("kind")
            is_item = oid in item_ot_ids or (
                oid not in task_ot_ids and parent_tt is not None and kind == "CHECKLIST"
            )
            if (
                parent_raw is None  # parent out of scope
                or parent_raw.get("parent_id")  # a deeper level
                or parent_tt is None  # parent not in TickTick yet: next pass
                or raw.get(UNREADABLE)
            ):
                w.ot.out_of_scope.add(oid)
                w.ot_items.out_of_scope.add(oid)
                continue
            if is_item:
                pair = item_ot_ids.get(oid)
                if pair is not None and pair.tt_project != parent_tt:
                    log.warning("OpenTodo subtask %s moved to another task; not synced", oid)
                    w.ot_items.out_of_scope.add(oid)
                elif self._items_in_scope(w, parent_tt):
                    w.ot_items.tasks[oid] = item_from_ot(raw, parent_tt)
                else:
                    w.ot_items.out_of_scope.add(oid)
                continue
            w.ot_raw[oid] = raw
            task = self._normalize(w.mapper.from_ot, raw, "OpenTodo", oid)
            if (
                task is None
                or task.project not in in_scope
                or parent_tt not in w.tt.tasks
                or (task.project in w.retiring and oid not in task_ot_ids)
            ):
                w.ot.out_of_scope.add(oid)
            else:
                w.ot.tasks[oid] = task

        for parent, raw in w.tt_raw.items():
            if parent in w.tt.tasks and self._items_in_scope(w, parent):
                for item in raw.get("items") or []:
                    if isinstance(item, dict) and item.get("id"):
                        w.tt_item_raw[str(item["id"])] = item
                        w.tt_items.tasks[str(item["id"])] = item_from_tt(item, parent)
        for p in w.item_pairs.values():
            if not self._items_in_scope(w, p.tt_project):
                w.tt_items.out_of_scope.add(p.tt_id)
                w.ot_items.out_of_scope.add(p.ot_id)

    def _resolve_missing_tt(self, w: World, in_scope: set[str]) -> None:
        """Paired TickTick tasks absent from the undone lists: completed, deleted, or moved?"""
        for p in w.pairs.values():
            if p.tt_id in w.tt.tasks or p.tt_id in w.tt.out_of_scope:
                continue
            if p.ot_id in w.ot.out_of_scope:
                continue
            ot_task = w.ot.tasks.get(p.ot_id)
            ot_changed = ot_task is not None and ot_task.content_hash() != p.ot_hash
            if p.base is not None and p.base.completed and not ot_changed:
                # Completed at the last sync and nothing to write back: not fetched again.
                # A changed OpenTodo copy is written to TickTick, so it's looked up first: a
                # deletion there would otherwise go unseen, and the write would fail every pass.
                w.tt.unchanged.add(p.tt_id)
                continue
            raw = self.tt.get_task(p.tt_project, p.tt_id)
            if raw is None or raw.get("deleted"):
                w.tt.deleted.add(p.tt_id)
                continue
            if raw.get("projectId"):
                w.tt_lists_alive.add(str(raw["projectId"]))
            task = self._normalize(w.mapper.from_tt, raw, "TickTick", p.tt_id)
            if task is None or (raw.get("parentId") and raw["parentId"] not in w.tt.tasks):
                w.tt.out_of_scope.add(p.tt_id)
            elif raw.get("status") in TT_DONE or task.project in in_scope:
                w.tt_raw[p.tt_id] = raw
                w.tt.tasks[p.tt_id] = task
            else:
                w.tt.out_of_scope.add(p.tt_id)

    @staticmethod
    def _normalize(fn: Callable[[JSON], Task], raw: JSON, side: str, task_id: str) -> Task | None:
        try:
            return fn(raw)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("skipping %s task %s: cannot normalize (%s)", side, task_id, exc)
            return None

    # --- run --------------------------------------------------------------------------

    def run(self, *, dry_run: bool, allow_bulk: bool = False) -> PassResult:
        res = PassResult(dry_run=dry_run)
        w = self.collect(dry_run=dry_run)
        res.ot_tasks_read = w.ot_tasks_read
        planned = self._plan_pass(w, res, dry_run=dry_run, allow_bulk=allow_bulk)
        # Lists and projects change only once the guards have passed. Created, renamed or
        # restored ones change what tasks map to, so the pass then starts over (once).
        if planned is not None and self._apply_projects(w, dry_run=dry_run):
            w = self.collect(dry_run=dry_run)
            res.ot_tasks_read = w.ot_tasks_read
            planned = self._plan_pass(w, res, dry_run=dry_run, allow_bulk=allow_bulk)
            if planned is not None:
                self._apply_projects(w, dry_run=dry_run)  # normally nothing left to do
        if planned is None:
            return res
        actions, item_actions = planned

        if not dry_run and (actions or item_actions):
            self.state.check_writable()
        for a in actions:
            self._apply(a, w, res, dry_run=dry_run)
        self._apply_items(item_actions, w, res, dry_run=dry_run)
        if not dry_run:
            self._retire_projects(w, res)
        try:
            HabitSync(
                self.tt,
                self.ot,
                self.state,
                timezone=w.mapper.default_tz,
                now_ms=self.now_ms,
                run_id=self.run_id,
                max_destructive=self.max_destructive,
                noted=self._habit_notes,
            ).run(
                w.ot_habits,
                w.ot_records,
                w.ot_project_ids,
                res,
                dry_run=dry_run,
                allow_bulk=allow_bulk,
            )
        except sqlite3.Error:
            raise  # see _apply
        except Exception as exc:
            res.errors += 1
            log.error("habit sync failed: %s", exc)  # noqa: TRY400
            log.debug("traceback", exc_info=True)

        if not dry_run:
            if dropped := self.state.drop_orphan_items():
                log.info("forgot %d checklist item pair(s) of tasks no longer synced", dropped)
            if res.blocked is None:  # the habit guard may have tripped after the task pass
                self.state.set_meta("blocked", None)
            deleted = res.tt.deleted + res.ot.deleted + res.tt_items.deleted
            if deleted and not allow_bulk:  # deletions approved with --allow-bulk don't count
                record_deletes(self.state, self.now_ms(), deleted)
            if res.changed or res.errors:
                self.state.set_meta_json("last_change", {"at": self.now_ms()} | res.to_json())
        return res

    def _plan_pass(
        self, w: World, res: PassResult, *, dry_run: bool, allow_bulk: bool
    ) -> tuple[list[Action], list[Action]] | None:
        """Task and checklist-item actions, or None when a guard stops the pass."""
        if self.state.pair_count() == 0 and not self.state.get_meta("matched_existing"):
            open_ot = sum(not t.completed for t in w.ot.tasks.values())
            if open_ot:
                res.refused = (
                    f"no task pairs yet but OpenTodo already has {open_ot} open task(s); "
                    "syncing now would duplicate them. "
                    "Empty OpenTodo or run --match-existing first."
                )
                log.error("%s", res.refused)
                if not dry_run:
                    return None

        actions = self._mark_tt_occurrences(engine.plan(w.pairs.values(), w.tt, w.ot), w)
        # Parents before their subtasks, so a new subtask's parent exists when it is created.
        actions.sort(key=lambda a: a.task is not None and a.task.parent is not None)
        item_actions = self._plan_items(w, actions)
        if allow_bulk:
            return actions, item_actions
        # Item deletions in TickTick (the source of truth) count; OpenTodo's copies don't.
        item_deletes = [a for a in item_actions if a.kind is Kind.DELETE_TT]
        n_destructive = engine.destructive_count(actions) + len(item_deletes)
        if n_destructive > self.max_destructive:
            self._block(
                res,
                f"{n_destructive} deletes/completions exceed "
                f"TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE={self.max_destructive}; "
                "nothing written. Review, then run once with --allow-bulk.",
                [self._describe(a, w) for a in actions if a.destructive]
                + [self._describe_item(a, w) for a in item_deletes],
                dry_run=dry_run,
            )
            return None
        # Over a day too: a fault deleting a few tasks every pass never trips the guard above.
        deletes = [a for a in actions if a.kind in (Kind.DELETE_OT, Kind.DELETE_TT)]
        if deletes or item_deletes:
            n = len(deletes) + len(item_deletes)
            recent = deletes_in_last_day(self.state, self.now_ms())
            if recent + n > self.max_deletes_per_day:
                self._block(
                    res,
                    f"{n} deletions on top of {recent} in the last 24 hours exceed "
                    f"TICKTICK_OPENTODO_SYNC_MAX_DELETES_PER_DAY={self.max_deletes_per_day}; "
                    "nothing written. Review, then run once with --allow-bulk.",
                    [self._describe(a, w) for a in deletes]
                    + [self._describe_item(a, w) for a in item_deletes],
                    dry_run=dry_run,
                )
                return None
        # Many new tasks at once, once pairs exist: most likely tasks whose pairs were lost
        # (an older state backup restored), about to be duplicated in both apps. Not on the
        # first seed, and not checklist items, which legitimately arrive in bulk.
        creates = [a for a in actions if a.kind in (Kind.CREATE_OT, Kind.CREATE_TT)]
        if len(creates) > self.max_creates and self.state.pair_count():
            self._block(
                res,
                f"{len(creates)} new tasks exceed "
                f"TICKTICK_OPENTODO_SYNC_MAX_CREATES={self.max_creates}; nothing written. "
                "Review (an older state backup restored?), then run once with --allow-bulk.",
                [self._describe(a, w) for a in creates],
                dry_run=dry_run,
            )
            return None
        return actions, item_actions

    def _block(self, res: PassResult, reason: str, blocked: list[str], *, dry_run: bool) -> None:
        """A guard tripped: log what it holds back and record it (``--status``, health)."""
        res.blocked = reason
        log.warning("%s", reason)
        for line in blocked:
            log.warning("  blocked: %s", line)
        if not dry_run:
            self.state.set_meta_json(
                "blocked", {"at": self.now_ms(), "count": len(blocked), "reason": reason}
            )

    def _mark_tt_occurrences(self, actions: list[Action], w: World) -> list[Action]:
        """Tell a repeating task completed in TickTick from one that was only moved.

        Completing moves the TickTick task on under the same id, like a date change; the
        difference is a completed copy with the old due date. When there is one, OpenTodo
        completes that occurrence (a record in its history) instead of just moving.
        """
        candidates: dict[str, tuple[Action, Task]] = {}
        for a in actions:
            base = w.pairs[a.tt_id].base if a.tt_id in w.pairs else None
            current = w.ot.tasks.get(a.ot_id or "")
            if (
                a.kind is Kind.UPDATE_OT
                and a.task is not None
                and base is not None
                and current is not None
                and _completable_rule(base.recurrence)
                and base.due is not None
                and a.task.due is not None
                and a.task.due.date > base.due.date
                and not a.task.completed
                and not current.completed
                and current.due == base.due  # OpenTodo still shows the occurrence
                and current.recurrence == base.recurrence
                # Completed in OpenTodo already (that flow went the other way).
                and base.due.date not in w.ot.completed_occurrences.get(a.ot_id or "", set())
            ):
                candidates[str(a.tt_id)] = (a, base)
        if not candidates:
            return actions

        found = self._tt_completed_copies(candidates, w)
        out = []
        for a in actions:
            hit = found.get(a.tt_id or "")
            if hit is not None:
                day, at = hit
                a = replace(
                    a,
                    ot_occurrence=day,
                    ot_occurrence_at=at,
                    note=f"repeating task completed in TickTick for {day}",
                )
            out.append(a)
        return out

    def _tt_completed_copies(
        self, candidates: dict[str, tuple[Action, Task]], w: World
    ) -> dict[str, tuple[str, int | None]]:
        """TickTick id -> (occurrence date, completed ms) for candidates with a completed copy."""
        now = datetime.fromtimestamp(self.now_ms() / 1000, UTC)
        by_project: dict[str, list[str]] = {}
        oldest: dict[str, date] = {}
        for tt_id, (_, base) in candidates.items():
            pid = str((w.tt_raw.get(tt_id) or {}).get("projectId") or w.pairs[tt_id].tt_project)
            by_project.setdefault(pid, []).append(tt_id)
            assert base.due is not None
            d = date.fromisoformat(base.due.date)
            oldest[pid] = min(oldest.get(pid, d), d)
        found: dict[str, tuple[str, int | None]] = {}
        for pid, tt_ids in by_project.items():
            # Completed at or after the old due day (a day early for zones), at most 30 days.
            start = max(
                datetime.combine(oldest[pid] - timedelta(days=1), datetime.min.time(), UTC),
                now - timedelta(days=30),
            )
            try:
                copies = self.tt.completed_tasks([pid], start, now + timedelta(minutes=5))
            except Exception as exc:
                log.warning("could not read TickTick's completed tasks (%s); dates just move", exc)
                continue
            if len(copies) >= 200:
                log.warning("TickTick returned 200 completed tasks for %s; some may be missed", pid)
            for tt_id in tt_ids:
                a, base = candidates[tt_id]
                assert base.due is not None and a.task is not None
                for c in copies:
                    if (
                        str(c.get("id")) != tt_id
                        and c.get("status") in TT_DONE
                        and str(c.get("projectId")) == pid
                        and norm_title(c.get("title")) == base.title
                        and (due := due_from_tt(c, w.mapper.default_tz)) is not None
                        and due.date == base.due.date
                    ):
                        found[tt_id] = (base.due.date, _ms(c.get("completedTime")))
                        break
        return found

    def _describe(self, a: Action, w: World) -> str:
        title = a.task.title if a.task else "?"
        project = w.project_names.get(a.task.project, a.task.project) if a.task else "?"
        flags = " [conflict]" if a.conflict else ""
        current = None
        if a.kind is Kind.UPDATE_OT and a.ot_id:
            current = w.ot.tasks.get(a.ot_id)
        elif a.kind is Kind.UPDATE_TT and a.tt_id:
            current = w.tt.tasks.get(a.tt_id)
        if current is not None and a.task is not None:
            flags += f" [{', '.join(changed_fields(current, a.task)) or 'no field change'}]"
        return (
            f"{a.kind.value} {title!r} in {project!r} (tt={a.tt_id} ot={a.ot_id}){flags}: {a.note}"
        )

    def _apply(self, a: Action, w: World, res: PassResult, *, dry_run: bool) -> None:
        desc = self._describe(a, w)
        if dry_run:
            if a.kind is not Kind.REHASH:
                log.info("DRY-RUN would %s", desc)
            _count(a, res)
            return
        handler = {
            Kind.CREATE_OT: self._create_ot,
            Kind.CREATE_TT: self._create_tt,
            Kind.UPDATE_OT: self._update_ot,
            Kind.UPDATE_TT: self._update_tt,
            Kind.DELETE_OT: self._delete_ot,
            Kind.DELETE_TT: self._delete_tt,
            Kind.FORGET: self._forget,
            Kind.REHASH: self._rehash,
        }[a.kind]
        try:
            handler(a, w)
        except sqlite3.Error:
            # The state couldn't record what was just written: stop the whole pass. Carrying
            # on would make more writes that can't be recorded either.
            raise
        except ChangedDuringPassError as exc:
            # Nothing written, nothing recorded: the next pass plans it again with the edit.
            log.info("left for the next pass: %s (%s)", desc, exc)
            return
        except Exception as exc:
            res.errors += 1
            log.error("failed to %s: %s", desc, exc)  # noqa: TRY400
            log.debug("traceback", exc_info=True)
            return
        if a.kind is not Kind.REHASH:
            (log.warning if a.conflict else log.info)("%s", desc)
        _count(a, res)

    # --- action handlers --------------------------------------------------------------

    def _ensure_labels(self, tags: Iterable[str], w: World) -> None:
        for tag in sorted(tags):
            if tag not in w.label_ids:
                created = self.ot.create_label(tag)
                w.label_ids[tag] = str(created["id"])
                w.mapper.label_names[w.label_ids[tag]] = tag

    def _create_ot(self, a: Action, w: World) -> None:
        assert a.tt_id and a.task
        self._ensure_labels(a.task.tags, w)
        body = w.mapper.to_ot(a.task, w.label_ids, currently_completed=False, now_ms=self.now_ms())
        resp = self.ot.create_task(
            body, idempotency_key=f"ticktick-opentodo-sync-{a.tt_id}-{self.run_id}"
        )
        ot_id = str(resp["id"])
        w.mapper.link_task(a.tt_id, ot_id)
        raw = w.tt_raw.get(a.tt_id) or {}
        readback = w.mapper.from_ot(resp)
        tt_hash = a.tt_hash or a.task.content_hash()
        if readback.reminders != a.task.reminders:
            tt_hash = ""  # reminders failed (logged): TickTick pushes them again next pass
        self.state.put_pair(
            Pair(
                tt_id=a.tt_id,
                ot_id=ot_id,
                tt_hash=tt_hash,
                ot_hash=readback.content_hash(),
                tt_project=str(raw.get("projectId") or w.mapper.tt_project_id(a.task.project)),
                base=a.task,
            )
        )

    def _create_tt(self, a: Action, w: World) -> None:
        assert a.ot_id and a.task
        body = w.mapper.to_tt(a.task)
        resp = self.tt.create_task(body)
        tt_id = str(resp["id"])
        w.tt_raw[tt_id] = resp
        w.mapper.link_task(tt_id, a.ot_id)
        readback = self._normalize(w.mapper.from_tt, resp, "TickTick", tt_id) or a.task
        self.state.put_pair(
            Pair(
                tt_id=tt_id,
                ot_id=a.ot_id,
                tt_hash=readback.content_hash(),
                ot_hash=a.ot_hash or a.task.content_hash(),
                tt_project=str(resp.get("projectId") or body["projectId"]),
                base=a.task,
            )
        )

    def _update_ot(self, a: Action, w: World) -> None:
        assert a.tt_id and a.ot_id and a.task
        current = w.ot.tasks[a.ot_id]
        if a.ot_occurrence:
            # Records the completion and moves the series on; the PATCH below then sets
            # TickTick's next date (and anything else that changed).
            try:
                self.ot.complete_occurrence(a.ot_id, a.ot_occurrence, a.ot_occurrence_at)
            except ApiError as exc:
                if exc.status != 400:
                    raise
                log.warning(
                    "OpenTodo refused to complete the %s occurrence (%s); moving the date only",
                    a.ot_occurrence,
                    exc,
                )
        self._ensure_labels(a.task.tags, w)
        body = w.mapper.to_ot(
            a.task, w.label_ids, currently_completed=current.completed, now_ms=self.now_ms()
        )
        resp = self.ot.update_task(a.ot_id, body)
        readback = w.mapper.from_ot(resp) if resp.get("id") else a.task
        # A rule TickTick only knows isn't sent; completing a repeating task clears it.
        rule = readback.recurrence if a.task.ticktick_only_rule else a.task.recurrence
        if a.task.completed and not current.completed:
            rule = None
        _check_taken("OpenTodo", replace(a.task, recurrence=rule), readback)
        self.state.update_pair(
            a.tt_id, ot_hash=readback.content_hash(), tt_hash=a.tt_hash, base=a.task
        )

    def _update_tt(self, a: Action, w: World) -> None:
        assert a.tt_id and a.task
        pair = w.pairs[a.tt_id]
        # Read again: the write sends the whole task back, checklist included. Built on the
        # copy from the start of the pass, it undid anything changed in TickTick since then.
        collected = w.tt_raw.get(a.tt_id)
        project = str((collected or {}).get("projectId") or pair.tt_project)
        raw = self.tt.get_task(project, a.tt_id)
        if raw is None:
            raise RuntimeError("TickTick task no longer exists")
        if collected is not None and _tt_view(w, raw) != _tt_view(w, collected):
            raise ChangedDuringPassError("changed in TickTick since this pass read it")
        target = w.mapper.tt_project_id(a.task.project)
        if raw.get("projectId") != target:
            self.tt.move_task(a.tt_id, str(raw.get("projectId") or pair.tt_project), target)
            raw = raw | {"projectId": target}
        current = w.mapper.from_tt(raw)
        reopen = current.completed and not a.task.completed
        complete = a.task.completed and not current.completed

        tt_hash = a.task.content_hash()
        if reopen or not current.same_content(a.task):
            body = w.mapper.to_tt(a.task, raw)
            if reopen:
                body |= {"status": 0, "completedTime": None}
            resp = self.tt.update_task(a.tt_id, body)
            if resp.get("id"):
                readback = w.mapper.from_tt(resp)
                if not complete:
                    tt_hash = readback.content_hash()
                # The update only reopens (a completion follows below), and the rule is left
                # alone when OpenTodo's can't be written to TickTick (warned in mapping).
                expected = replace(
                    a.task,
                    completed=current.completed and not reopen,
                    recurrence=readback.recurrence,
                )
                _check_taken("TickTick", expected, readback)
        if complete:
            # Record the *completed* content: a recurring task comes back open with the next
            # due date, and that must then read as a TickTick change to push to OpenTodo.
            self.tt.complete_task(target, a.tt_id)
        self.state.update_pair(
            a.tt_id, tt_hash=tt_hash, ot_hash=a.ot_hash, tt_project=target, base=a.task
        )
        if a.occurrence:
            # OpenTodo completed this occurrence's subtasks along with it; TickTick's checklist
            # for the next occurrence is what counts, so it wins on the next pass.
            self.state.resync_items_from_ticktick(a.tt_id)

    def _delete_ot(self, a: Action, w: World) -> None:
        assert a.tt_id and a.ot_id
        self.ot.delete_task(a.ot_id)
        self.state.delete_pair(a.tt_id)

    def _delete_tt(self, a: Action, w: World) -> None:
        assert a.tt_id
        self.tt.delete_task(w.pairs[a.tt_id].tt_project, a.tt_id)
        self.state.delete_pair(a.tt_id)

    def _forget(self, a: Action, w: World) -> None:
        assert a.tt_id
        self.state.delete_pair(a.tt_id)

    def _rehash(self, a: Action, w: World) -> None:
        assert a.tt_id
        self.state.update_pair(a.tt_id, tt_hash=a.tt_hash, ot_hash=a.ot_hash, base=a.task)

    # --- checklist items <-> OpenTodo subtasks ----------------------------------------

    def _plan_items(self, w: World, task_actions: list[Action]) -> list[Action]:
        actions = engine.plan(
            w.item_pairs.values(),
            w.tt_items,
            w.ot_items,
            create_completed=True,
            completion_sticks=False,
        )
        # A repeating parent completed in OpenTodo this pass: its items are resynced from
        # TickTick once TickTick has moved on (see _update_tt), not now.
        skip = {a.tt_id for a in task_actions if a.occurrence}
        out = []
        for a in actions:
            if self._item_parent(a, w) in skip:
                continue
            pair = w.item_pairs.get(a.tt_id or "")
            if pair is not None and pair.tt_hash == pair.ot_hash == "" and a.conflict:
                # Forced by resync_items_from_ticktick, not edits on both sides.
                a = replace(a, conflict=False, note="resync from TickTick after a repeat")
            out.append(a)
        return out

    @staticmethod
    def _item_parent(a: Action, w: World) -> str:
        pair = w.item_pairs.get(a.tt_id or "")
        if pair is not None:
            return pair.tt_project
        assert a.task is not None
        return a.task.project

    def _describe_item(self, a: Action, w: World) -> str:
        parent = self._item_parent(a, w)
        parent_title = w.tt.tasks[parent].title if parent in w.tt.tasks else parent
        title = a.task.title if a.task else "?"
        state = " [done]" if a.task and a.task.completed else ""
        flags = " [conflict]" if a.conflict else ""
        return f"{a.kind.value} item {title!r}{state} of {parent_title!r}{flags}: {a.note}"

    def _apply_items(
        self, actions: list[Action], w: World, res: PassResult, *, dry_run: bool
    ) -> None:
        to_tt: dict[str, list[Action]] = {}
        for a in actions:
            if dry_run:
                if a.kind is not Kind.REHASH:
                    log.info("DRY-RUN would %s", self._describe_item(a, w))
                _count_item(a, res)
            elif a.kind in (Kind.CREATE_TT, Kind.UPDATE_TT, Kind.DELETE_TT):
                to_tt.setdefault(self._item_parent(a, w), []).append(a)
            else:
                self._apply_item_ot(a, w, res)
        for parent, group in to_tt.items():
            try:
                self._apply_items_tt(parent, group, w)
            except sqlite3.Error:
                raise  # see _apply
            except Exception as exc:
                res.errors += len(group)
                log.error("failed to update checklist of %s: %s", parent, exc)  # noqa: TRY400
                log.debug("traceback", exc_info=True)
                continue
            for a in group:
                log.info("%s", self._describe_item(a, w))
                _count_item(a, res)

    def _apply_item_ot(self, a: Action, w: World, res: PassResult) -> None:
        try:
            if a.kind is Kind.CREATE_OT:
                self._create_item_ot(a, w)
            elif a.kind is Kind.UPDATE_OT:
                assert a.tt_id and a.ot_id and a.task
                body: JSON = {"title": a.task.title}
                current = w.ot_items.tasks[a.ot_id]
                if a.task.completed != current.completed:
                    body["completed_at"] = self.now_ms() if a.task.completed else None
                resp = self.ot.update_task(a.ot_id, body)
                readback = item_from_ot(resp, a.task.project) if resp.get("id") else a.task
                self.state.update_pair(
                    a.tt_id,
                    ot_hash=readback.content_hash(),
                    tt_hash=a.tt_hash,
                    base=a.task,
                    table=ITEMS,
                )
            elif a.kind is Kind.DELETE_OT:
                assert a.tt_id and a.ot_id
                self.ot.delete_task(a.ot_id)
                self.state.delete_pair(a.tt_id, table=ITEMS)
            elif a.kind is Kind.FORGET:
                assert a.tt_id
                self.state.delete_pair(a.tt_id, table=ITEMS)
            elif a.kind is Kind.REHASH:
                assert a.tt_id
                self.state.update_pair(
                    a.tt_id, tt_hash=a.tt_hash, ot_hash=a.ot_hash, base=a.task, table=ITEMS
                )
        except sqlite3.Error:
            raise  # see _apply
        except Exception as exc:
            res.errors += 1
            log.error("failed to %s: %s", self._describe_item(a, w), exc)  # noqa: TRY400
            log.debug("traceback", exc_info=True)
            return
        if a.kind is not Kind.REHASH:
            log.info("%s", self._describe_item(a, w))
        _count_item(a, res)

    def _create_item_ot(self, a: Action, w: World) -> None:
        assert a.tt_id and a.task
        parent_tt = a.task.project
        parent_ot = w.mapper.tt_to_ot_task[parent_tt]
        parent_raw = w.ot_raw.get(parent_ot) or {}
        item = w.tt_item_raw.get(a.tt_id) or {}
        body: JSON = {
            "title": a.task.title,
            "parent_id": parent_ot,
            "project_id": parent_raw.get("project_id"),
        }
        if isinstance(item.get("sortOrder"), int | float):
            body["position"] = item["sortOrder"]
        if a.task.completed:
            body["completed_at"] = self.now_ms()
        resp = self.ot.create_task(
            body, idempotency_key=f"ticktick-opentodo-sync-item-{a.tt_id}-{self.run_id}"
        )
        self.state.put_pair(
            Pair(
                tt_id=a.tt_id,
                ot_id=str(resp["id"]),
                tt_hash=a.tt_hash or a.task.content_hash(),
                ot_hash=item_from_ot(resp, parent_tt).content_hash(),
                tt_project=parent_tt,
                base=a.task,
            ),
            table=ITEMS,
        )

    def _apply_items_tt(self, parent: str, group: list[Action], w: World) -> None:
        """All of one task's item changes in one TickTick write (items live in the task)."""
        pair = w.pairs[parent]
        project = str((w.tt_raw.get(parent) or {}).get("projectId") or pair.tt_project)
        raw = self.tt.get_task(project, parent)  # fresh: a task action may have written it
        if raw is None or raw.get("status") in TT_DONE:
            raise RuntimeError("the TickTick task is gone or no longer open")
        items: list[JSON] = [dict(i) for i in raw.get("items") or [] if isinstance(i, dict)]
        by_id = {str(i.get("id")): i for i in items}
        old_ids = set(by_id)
        creates: list[Action] = []
        next_order = max((i.get("sortOrder") or 0 for i in items), default=0)
        for a in group:
            assert a.tt_id or a.kind is Kind.CREATE_TT
            if a.kind is Kind.DELETE_TT:
                items = [i for i in items if str(i.get("id")) != a.tt_id]
            elif a.kind is Kind.UPDATE_TT:
                assert a.task and a.tt_id
                if (item := by_id.get(a.tt_id)) is None:
                    raise RuntimeError(f"checklist item {a.tt_id} no longer exists")
                item["title"] = a.task.title
                item["status"] = TT_ITEM_DONE if a.task.completed else 0
            else:
                assert a.task
                next_order += 1
                status = TT_ITEM_DONE if a.task.completed else 0
                items.append({"title": a.task.title, "status": status, "sortOrder": next_order})
                creates.append(a)
        body = {k: v for k, v in raw.items() if k not in TT_DROP_ON_MERGE} | {"items": items}
        resp = self.tt.update_task(parent, body)
        if not isinstance(resp.get("items"), list):
            resp = self.tt.get_task(project, parent) or {}
        after = {str(i.get("id")): i for i in resp.get("items") or [] if isinstance(i, dict)}
        new_items = [i for iid, i in after.items() if iid not in old_ids]
        for a in group:
            if a.kind is Kind.DELETE_TT:
                assert a.tt_id
                self.state.delete_pair(a.tt_id, table=ITEMS)
            elif a.kind is Kind.UPDATE_TT:
                assert a.tt_id and a.task
                got = after.get(a.tt_id)
                tt_hash = item_from_tt(got, parent).content_hash() if got else ""
                self.state.update_pair(
                    a.tt_id, tt_hash=tt_hash, ot_hash=a.ot_hash, base=a.task, table=ITEMS
                )
        for a in creates:
            assert a.task and a.ot_id
            match = next(
                (i for i in new_items if (i.get("title") or "").strip() == a.task.title), None
            )
            if match is None:
                raise RuntimeError(f"TickTick did not return the new item {a.task.title!r}")
            new_items.remove(match)
            self.state.put_pair(
                Pair(
                    tt_id=str(match["id"]),
                    ot_id=a.ot_id,
                    tt_hash=item_from_tt(match, parent).content_hash(),
                    ot_hash=a.ot_hash or a.task.content_hash(),
                    tt_project=parent,
                    base=a.task,
                ),
                table=ITEMS,
            )

    # --- reports ----------------------------------------------------------------------

    def recurrence_report(self) -> RecurrenceReport:
        """How each paired repeating TickTick task's rule would translate. Writes nothing."""
        w = self.collect(dry_run=True)
        rep = RecurrenceReport()
        for p in sorted(w.pairs.values(), key=lambda p: p.tt_id):
            raw, task = w.tt_raw.get(p.tt_id), w.tt.tasks.get(p.tt_id)
            if raw is None or task is None or not raw.get("repeatFlag"):
                continue
            due = date.fromisoformat(task.due.date) if task.due else None
            t = recurrence.from_ticktick(str(raw["repeatFlag"]), raw.get("repeatFrom"), due)
            where = w.project_names.get(task.project, task.project)
            label = f"{task.title!r} in {where!r} (due {due or '-'})"
            source = f"{raw['repeatFlag']} from={raw.get('repeatFrom')}"
            current = (w.ot_raw.get(p.ot_id) or {}).get("recurrence")
            if t.rule is None:
                rep.untranslatable.append((label, source, t.problem))
                continue
            rep.translated[(source, t.rule)] += 1
            if t.problem:
                rep.warnings.append((label, t.rule, t.problem))
            if current and current != t.rule:
                rep.differs.append((label, str(current), t.rule))
            elif current:
                rep.already += 1
        return rep

    # --- one-time pairing -------------------------------------------------------------

    def match_existing(self, *, dry_run: bool) -> engine.MatchResult:
        w = self.collect(dry_run=dry_run)
        m = engine.match_existing(w.pairs.values(), w.tt, w.ot)

        def label(snap: Snapshot, task_id: str) -> str:
            t = snap.tasks[task_id]
            return f"{t.title!r} in {w.project_names.get(t.project, t.project)!r}"

        log.info("match-existing: %d pair(s) by project + exact title", len(m.matches))
        for tt_id in m.tt_unmatched:
            log.info(
                "  unmatched TickTick task (sync would create it in OpenTodo): %s",
                label(w.tt, tt_id),
            )
        for ot_id in m.ot_unmatched:
            log.warning(
                "  unmatched OpenTodo task (sync would create it in TickTick): %s",
                label(w.ot, ot_id),
            )
        for project, title in m.ambiguous:
            log.warning(
                "  ambiguous title %r in %r (duplicates; not paired)",
                title,
                w.project_names.get(project, project),
            )

        if dry_run:
            log.info("DRY-RUN: nothing recorded")
            return m
        for tt_id, ot_id in m.matches:
            # Empty TickTick hash => next sync sees "changed in TickTick" and TickTick wins.
            self.state.put_pair(
                Pair(
                    tt_id=tt_id,
                    ot_id=ot_id,
                    tt_hash="",
                    ot_hash=w.ot.tasks[ot_id].content_hash(),
                    tt_project=str(w.tt_raw[tt_id].get("projectId")),
                    base=None,
                )
            )
        self.state.set_meta("matched_existing", "1")
        return m


@dataclass
class RecurrenceReport:
    translated: Counter[tuple[str, str]] = field(default_factory=Counter)
    """(TickTick rule, OpenTodo rule) -> number of tasks."""
    untranslatable: list[tuple[str, str, str]] = field(default_factory=list)
    """(task, TickTick rule, reason)."""
    warnings: list[tuple[str, str, str]] = field(default_factory=list)
    """(task, OpenTodo rule, warning): translated, but worth a look."""
    differs: list[tuple[str, str, str]] = field(default_factory=list)
    """(task, OpenTodo's current rule, translation): OpenTodo already repeats differently."""
    already: int = 0
    """OpenTodo twin already has exactly the translated rule."""

    def lines(self) -> list[str]:
        n = sum(self.translated.values())
        out = [
            f"recurrence report: {n + len(self.untranslatable)} repeating paired task(s): "
            f"{n} translate, {len(self.untranslatable)} don't, {len(self.warnings)} warning(s), "
            f"{self.already} already set in OpenTodo, {len(self.differs)} set differently. "
            "Nothing was written.",
            "translations (tasks  TickTick rule  ->  OpenTodo rule):",
        ]
        for (src, rule), count in sorted(self.translated.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append(f"  {count:4}  {src}  ->  {rule}")
        if self.untranslatable:
            out.append("untranslatable (would keep today's behaviour: due date only):")
            out += [f"  {label}: {src}: {why}" for label, src, why in self.untranslatable]
        if self.warnings:
            out.append("warnings:")
            out += [f"  {label}: {rule!r}: {why}" for label, rule, why in self.warnings]
        if self.differs:
            out.append("OpenTodo already repeats differently:")
            out += [
                f"  {label}: has {cur!r}, TickTick says {new!r}" for label, cur, new in self.differs
            ]
        return out


def _count_item(a: Action, res: PassResult) -> None:
    res.conflicts += a.conflict
    side = (
        res.ot_items if a.kind in (Kind.CREATE_OT, Kind.UPDATE_OT, Kind.DELETE_OT) else res.tt_items
    )
    if a.kind in (Kind.CREATE_OT, Kind.CREATE_TT):
        side.created += 1
    elif a.kind in (Kind.UPDATE_OT, Kind.UPDATE_TT):
        side.updated += 1
    elif a.kind in (Kind.DELETE_OT, Kind.DELETE_TT):
        side.deleted += 1


def _count(a: Action, res: PassResult) -> None:
    res.conflicts += a.conflict
    side = res.ot if a.kind in (Kind.CREATE_OT, Kind.UPDATE_OT, Kind.DELETE_OT) else res.tt
    if a.kind in (Kind.CREATE_OT, Kind.CREATE_TT):
        side.created += 1
    elif a.kind in (Kind.UPDATE_OT, Kind.UPDATE_TT):
        side.updated += 1
        side.completed += a.completes
    elif a.kind in (Kind.DELETE_OT, Kind.DELETE_TT):
        side.deleted += 1


DAY_MS = 24 * 60 * 60 * 1000
RECENT_DELETES = "recent_deletes"
"""Meta key: ``[[at_ms, count], ...]`` of the passes that deleted, for the daily budget."""


def _recent_deletes(state: State, now_ms: int) -> list[list[int]]:
    entries = state.get_meta_json(RECENT_DELETES) or []
    return [
        [int(e[0]), int(e[1])]
        for e in entries
        if isinstance(e, list) and len(e) == 2 and int(e[0]) > now_ms - DAY_MS
    ]


def deletes_in_last_day(state: State, now_ms: int) -> int:
    """Deletions applied in the last 24 hours, except those approved with --allow-bulk."""
    return sum(n for _, n in _recent_deletes(state, now_ms))


def record_deletes(state: State, now_ms: int, count: int) -> None:
    """Add a pass's deletions (written only by passes that deleted: SD card)."""
    state.set_meta_json(RECENT_DELETES, [*_recent_deletes(state, now_ms), [now_ms, count]])


def _check_taken(side: str, sent: Task, got: Task) -> None:
    """Warn about fields a write didn't change. The readback's hash is what gets recorded (so
    the sync never mistakes its own write for an edit), so an ignored field left the two
    apps different without a word."""
    if fields := changed_fields(sent, got):
        log.warning(
            "%s didn't take the %s of %r: the apps differ there until either side changes it",
            side,
            ", ".join(fields),
            sent.title,
        )


def _tt_view(w: World, raw: JSON) -> tuple[Task, list[tuple[str, str, object]]]:
    """What a whole-task TickTick write would overwrite: the synced content and the checklist.

    Normalized, so the list and single-task endpoints compare equal when nothing changed.
    """
    items = [
        (str(i.get("id")), str(i.get("title") or ""), i.get("status"))
        for i in raw.get("items") or []
        if isinstance(i, dict)
    ]
    return w.mapper.from_tt(raw), sorted(items, key=lambda i: i[0])


def _completable_rule(rule: str | None) -> bool:
    """OpenTodo completes occurrences of a rule it holds, except 'after completion' ones."""
    if not rule:
        return False
    return not rule.startswith(TICKTICK_ONLY_PREFIX) and "after completion" not in rule


def _ms(value: object) -> int | None:
    if not value:
        return None
    try:
        return int(parse_tt_datetime(str(value)).timestamp() * 1000)
    except ValueError:
        return None


def _damaged(snap: Snapshot, raw: JSON, paired: Iterable[str]) -> bool:
    """Flag a paired task holding the create placeholder; ``False`` = leave it alone.

    Paired: the engine repairs it (see ``Snapshot.damaged``). Unpaired: a create that was
    interrupted before it was recorded, which only a person should delete.
    """
    if raw.get(UNREADABLE) or PLACEHOLDER not in (raw.get("title"), raw.get("description")):
        return True
    oid = str(raw["id"])
    if oid in paired:
        snap.damaged.add(oid)
        return True
    log.warning(
        "OpenTodo task %s: left over from an interrupted create (placeholder content, "
        "not paired); delete it in OpenTodo",
        oid,
    )
    return False


def _unreadable(obj: JSON, content_field: str) -> bool:
    """Content the OpenTodo client could not decrypt, or a create that never got its content."""
    return bool(obj.get(UNREADABLE)) or obj.get(content_field) == PLACEHOLDER
