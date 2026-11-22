"""One sync pass: fetch both sides, reconcile projects, plan, guard, apply, record."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from . import engine
from .engine import Action, Kind, Pair, Snapshot
from .http import ApiError
from .mapping import TT_DONE, Mapper, ProjectInfo, reconcile_projects
from .models import INBOX, JSON, PENDING_PREFIX, Task, changed_fields
from .opentodo import PLACEHOLDER, UNREADABLE
from .state import State

log = logging.getLogger(__name__)


class TickTickApi(Protocol):
    def preference_timezone(self) -> str | None: ...
    def projects(self) -> list[JSON]: ...
    def project_data(self, project_id: str) -> JSON: ...
    def get_task(self, project_id: str, task_id: str) -> JSON | None: ...
    def create_task(self, body: JSON) -> JSON: ...
    def update_task(self, task_id: str, body: JSON) -> JSON: ...
    def complete_task(self, project_id: str, task_id: str) -> None: ...
    def delete_task(self, project_id: str, task_id: str) -> None: ...
    def move_task(self, task_id: str, from_project: str, to_project: str) -> None: ...
    def create_project(self, name: str) -> JSON: ...


class OpenTodoApi(Protocol):
    def begin_pass(self) -> None: ...
    def projects(self) -> list[JSON]: ...
    def create_project(self, name: str) -> JSON: ...
    def labels(self) -> list[JSON]: ...
    def create_label(self, name: str) -> JSON: ...
    def tasks(self) -> list[JSON]: ...
    def create_task(self, body: JSON, *, idempotency_key: str) -> JSON: ...
    def update_task(self, task_id: str, body: JSON) -> JSON: ...
    def delete_task(self, task_id: str) -> None: ...


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
    conflicts: int = 0
    errors: int = 0
    blocked: str | None = None
    """Bulk-delete guard tripped; nothing was written."""
    refused: str | None = None
    """First-sync guard tripped; nothing was written."""

    @property
    def changed(self) -> bool:
        return any(asdict(self.tt).values()) or any(asdict(self.ot).values())

    @property
    def ok(self) -> bool:
        return not (self.errors or self.blocked or self.refused)

    def summary(self) -> str:
        prefix = "DRY-RUN " if self.dry_run else ""
        line = (
            f"{prefix}TickTick: {self.tt} | OpenTodo: {self.ot} | "
            f"conflicts={self.conflicts} errors={self.errors}"
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
    label_ids: dict[str, str] = field(default_factory=dict)
    """Lowercased label name -> OpenTodo label id."""
    project_names: dict[str, str] = field(default_factory=dict)
    """Project key -> display name (for logs)."""


class SyncRefusedError(Exception):
    pass


class Syncer:
    def __init__(
        self,
        tt: TickTickApi,
        ot: OpenTodoApi,
        state: State,
        *,
        timezone: str | None = None,
        max_destructive: int = 10,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.tt = tt
        self.ot = ot
        self.state = state
        self.timezone = timezone
        self.max_destructive = max_destructive
        self.now_ms = now_ms
        self.run_id = str(now_ms())

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
        for p in self.ot.projects():
            if not p.get("id") or p.get("deleted_at") is not None:
                continue
            if _unreadable(p, "name"):
                # Kept (so a stored pair survives) but inactive: its tasks stay out of scope
                # and its name is never matched or copied.
                log.warning("OpenTodo project %s: name unreadable; leaving it alone", p["id"])
                ot_projects[str(p["id"])] = ProjectInfo(f"<unreadable {p['id']}>", False)
            else:
                ot_projects[str(p["id"])] = ProjectInfo(
                    str(p.get("name") or ""), not p.get("archived")
                )
        project_map = self._reconcile_projects(tt_projects, ot_projects, dry_run=dry_run)
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
        )
        self._collect_tt(world, [pid for pid in tt_projects if pid in in_scope], in_scope, dry_run)
        self._collect_ot(world, in_scope)
        self._resolve_missing_tt(world, in_scope)
        return world

    def _reconcile_projects(
        self,
        tt_projects: dict[str, ProjectInfo],
        ot_projects: dict[str, ProjectInfo],
        *,
        dry_run: bool,
    ) -> dict[str, str]:
        plan = reconcile_projects(self.state.project_pairs(), tt_projects, ot_projects)
        pairs = dict(plan.pairs)
        verb = "would " if dry_run else ""
        for tt_id in plan.stale:
            log.info("%sforget project pair for vanished list %s", verb, tt_id)
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
                pairs[tt_id] = str(created["id"])
                ot_projects[pairs[tt_id]] = ProjectInfo(name, True)
                self.state.add_project_pair(tt_id, pairs[tt_id], name)
        for ot_id, name in plan.create_in_tt:
            log.info("%screate TickTick list %r", verb, name)
            if not dry_run:
                created = self.tt.create_project(name)
                tt_id = str(created["id"])
                pairs[tt_id] = ot_id
                tt_projects[tt_id] = ProjectInfo(name, True)
                self.state.add_project_pair(tt_id, ot_id, name)
        return pairs

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

        for tid, raw in w.tt_raw.items():
            task = self._normalize(w.mapper.from_tt, raw, "TickTick", tid)
            if task is None or raw.get("parentId") or task.project not in in_scope:
                w.tt.out_of_scope.add(tid)
            else:
                w.tt.tasks[tid] = task

    def _collect_ot(self, w: World, in_scope: set[str]) -> None:
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

        for raw in self.ot.tasks():
            oid = str(raw.get("id") or "")
            if not oid:
                continue
            if raw.get("deleted_at") is not None:
                w.ot.deleted.add(oid)
                continue
            if raw.get("parent_id") or raw.get("series_id") or raw.get("habit"):
                w.ot.out_of_scope.add(oid)
                continue
            if _unreadable(raw, "title") or unreadable_labels.intersection(raw.get("labels") or []):
                # Never let unreadable content look like an edit (or a deletion).
                log.warning("OpenTodo task %s: content or a label unreadable; skipping it", oid)
                w.ot.out_of_scope.add(oid)
                continue
            task = self._normalize(w.mapper.from_ot, raw, "OpenTodo", oid)
            if task is None or task.project not in in_scope:
                w.ot.out_of_scope.add(oid)
            else:
                w.ot.tasks[oid] = task

    def _resolve_missing_tt(self, w: World, in_scope: set[str]) -> None:
        """Paired TickTick tasks absent from the undone lists: completed, deleted, or moved?"""
        for p in w.pairs.values():
            if p.tt_id in w.tt.tasks or p.tt_id in w.tt.out_of_scope:
                continue
            if p.ot_id in w.ot.out_of_scope:
                continue
            if p.base is not None and p.base.completed:
                w.tt.unchanged.add(p.tt_id)
                continue
            raw = self.tt.get_task(p.tt_project, p.tt_id)
            if raw is None or raw.get("deleted"):
                w.tt.deleted.add(p.tt_id)
                continue
            task = self._normalize(w.mapper.from_tt, raw, "TickTick", p.tt_id)
            if task is None or raw.get("parentId"):
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
                    return res

        actions = engine.plan(w.pairs.values(), w.tt, w.ot)
        n_destructive = engine.destructive_count(actions)
        if n_destructive > self.max_destructive and not allow_bulk:
            res.blocked = (
                f"{n_destructive} deletes/completions exceed "
                f"TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE={self.max_destructive}; "
                "nothing written. Review, then run once with --allow-bulk."
            )
            log.warning("%s", res.blocked)
            for a in actions:
                if a.destructive:
                    log.warning("  blocked: %s", self._describe(a, w))
            if not dry_run:
                self.state.set_meta_json(
                    "blocked", {"at": self.now_ms(), "count": n_destructive, "reason": res.blocked}
                )
            return res

        for a in actions:
            self._apply(a, w, res, dry_run=dry_run)

        if not dry_run:
            self.state.set_meta("blocked", None)
            if res.changed or res.errors:
                self.state.set_meta_json("last_change", {"at": self.now_ms()} | res.to_json())
        return res

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
        raw = w.tt_raw.get(a.tt_id) or {}
        self.state.put_pair(
            Pair(
                tt_id=a.tt_id,
                ot_id=ot_id,
                tt_hash=a.tt_hash or a.task.content_hash(),
                ot_hash=w.mapper.from_ot(resp).content_hash(),
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
        self._ensure_labels(a.task.tags, w)
        body = w.mapper.to_ot(
            a.task, w.label_ids, currently_completed=current.completed, now_ms=self.now_ms()
        )
        resp = self.ot.update_task(a.ot_id, body)
        readback = w.mapper.from_ot(resp) if resp.get("id") else a.task
        self.state.update_pair(
            a.tt_id, ot_hash=readback.content_hash(), tt_hash=a.tt_hash, base=a.task
        )

    def _update_tt(self, a: Action, w: World) -> None:
        assert a.tt_id and a.task
        pair = w.pairs[a.tt_id]
        raw = w.tt_raw.get(a.tt_id) or self.tt.get_task(pair.tt_project, a.tt_id)
        if raw is None:
            raise RuntimeError("TickTick task no longer exists")
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
            if resp.get("id") and not complete:
                tt_hash = w.mapper.from_tt(resp).content_hash()
        if complete:
            # Record the *completed* content: a recurring task comes back open with the next
            # due date, and that must then read as a TickTick change to push to OpenTodo.
            self.tt.complete_task(target, a.tt_id)
        self.state.update_pair(
            a.tt_id, tt_hash=tt_hash, ot_hash=a.ot_hash, tt_project=target, base=a.task
        )

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


def _unreadable(obj: JSON, content_field: str) -> bool:
    """Content the OpenTodo client could not decrypt, or a create that never got its content."""
    return bool(obj.get(UNREADABLE)) or obj.get(content_field) == PLACEHOLDER
