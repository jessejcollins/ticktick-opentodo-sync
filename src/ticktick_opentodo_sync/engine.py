"""Pure sync rules: previous state + both snapshots in, actions out. No I/O."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from .models import Task


class Kind(Enum):
    CREATE_OT = "create_ot"
    CREATE_TT = "create_tt"
    UPDATE_OT = "update_ot"
    UPDATE_TT = "update_tt"
    DELETE_OT = "delete_ot"
    DELETE_TT = "delete_tt"
    FORGET = "forget"
    """Both sides gone: drop the pair."""
    REHASH = "rehash"
    """Both sides changed to the same content: record the new hashes, write nothing."""


@dataclass(frozen=True, slots=True)
class Pair:
    tt_id: str
    ot_id: str
    tt_hash: str
    ot_hash: str
    tt_project: str
    """Real TickTick project id the task was last seen in (needed for GET/DELETE)."""
    base: Task | None = None
    """Content at the last sync; ``None`` for pairs made by --match-existing."""


@dataclass
class Snapshot:
    """One side's current view.

    A paired id that is in none of these sets is treated as deleted.
    """

    tasks: dict[str, Task] = field(default_factory=dict)
    deleted: set[str] = field(default_factory=set)
    unchanged: set[str] = field(default_factory=set)
    """Present but not fetched (TickTick tasks already known to be completed)."""
    out_of_scope: set[str] = field(default_factory=set)
    completed_occurrences: dict[str, set[str]] = field(default_factory=dict)
    """OpenTodo only: repeating task id -> dates of its completed occurrence records."""
    damaged: set[str] = field(default_factory=set)
    """OpenTodo only: paired ids whose title/notes are the sync's create placeholder.

    An open browser can save its encrypted copy of the placeholder over the content the
    sync PATCHed right after the create. The placeholder is never an edit: the content
    from the last sync stands in for it, and OpenTodo is rewritten.
    """


REPAIR_NOTE = "OpenTodo held the create placeholder: rewritten"


@dataclass(frozen=True, slots=True)
class Action:
    kind: Kind
    tt_id: str | None = None
    ot_id: str | None = None
    task: Task | None = None
    """Desired content (create/update) or the new base (rehash)."""
    tt_hash: str | None = None
    """For writes to OpenTodo: TickTick-side hash to record. ``None`` = leave as is."""
    ot_hash: str | None = None
    """For writes to TickTick: OpenTodo-side hash to record. ``None`` = leave as is."""
    conflict: bool = False
    completes: bool = False
    """The write marks an open task completed (counts towards the bulk guard)."""
    occurrence: bool = False
    """Completes the TickTick instance of a repeating task completed in OpenTodo."""
    ot_occurrence: str | None = None
    """UPDATE_OT only: first complete this date's OpenTodo occurrence (the repeating task was
    completed in TickTick, which moved it on); set by the sync, which can see TickTick's
    completed copies."""
    ot_occurrence_at: int | None = None
    """When TickTick says it was completed (ms)."""
    note: str = ""

    @property
    def destructive(self) -> bool:
        return self.kind in (Kind.DELETE_OT, Kind.DELETE_TT) or self.completes


def plan(
    pairs: Iterable[Pair],
    tt: Snapshot,
    ot: Snapshot,
    *,
    create_completed: bool = False,
    completion_sticks: bool = True,
) -> list[Action]:
    """Actions for tasks (defaults) or checklist items.

    Items are created even when already checked (``create_completed``), and on a conflict
    TickTick wins outright, checked state included (``completion_sticks=False``): a repeating
    TickTick checklist resets its items, which must not be re-checked from OpenTodo.
    """
    actions: list[Action] = []
    pairs = list(pairs)
    paired_tt = {p.tt_id for p in pairs}
    paired_ot = {p.ot_id for p in pairs}

    for p in sorted(pairs, key=lambda p: p.tt_id):
        actions.extend(_plan_pair(p, tt, ot, completion_sticks=completion_sticks))

    for tt_id, task in sorted(tt.tasks.items()):
        if tt_id not in paired_tt and (create_completed or not task.completed):
            actions.append(
                Action(
                    Kind.CREATE_OT,
                    tt_id=tt_id,
                    task=task,
                    tt_hash=task.content_hash(),
                    note="new in TickTick",
                )
            )
    for ot_id, task in sorted(ot.tasks.items()):
        if ot_id not in paired_ot and (create_completed or not task.completed):
            actions.append(
                Action(
                    Kind.CREATE_TT,
                    ot_id=ot_id,
                    task=task,
                    ot_hash=task.content_hash(),
                    note="new in OpenTodo",
                )
            )
    return actions


def _plan_pair(
    p: Pair, tt: Snapshot, ot: Snapshot, *, completion_sticks: bool = True
) -> list[Action]:
    if p.tt_id in tt.out_of_scope or p.ot_id in ot.out_of_scope:
        return []
    ot_task = ot.tasks.get(p.ot_id)
    if ot_task is None or p.ot_id not in ot.damaged:
        return _plan_rules(p, tt, ot, completion_sticks=completion_sticks)
    return _plan_repair(p, ot_task, tt, ot, completion_sticks=completion_sticks)


def _plan_repair(
    p: Pair, damaged: Task, tt: Snapshot, ot: Snapshot, *, completion_sticks: bool
) -> list[Action]:
    """Plan as if OpenTodo still held the content from the last sync, then rewrite it."""
    tt_now = p.base if p.tt_id in tt.unchanged else tt.tasks.get(p.tt_id)
    source = p.base or tt_now
    if source is None:  # nothing to repair from (and TickTick is gone: delete as usual)
        return _plan_rules(p, tt, ot, completion_sticks=completion_sticks)
    repaired = replace(damaged, title=source.title, notes=source.notes)
    view = replace(ot, tasks={**ot.tasks, p.ot_id: repaired})
    out = _plan_rules(p, tt, view, completion_sticks=completion_sticks)
    if any(a.kind in (Kind.UPDATE_OT, Kind.DELETE_OT) or a.occurrence for a in out):
        # Already rewrites or removes OpenTodo (or a repeat moves on first: next pass).
        return [
            replace(a, note=f"{a.note}; {REPAIR_NOTE}") if a.kind is Kind.UPDATE_OT else a
            for a in out
        ]
    if tt_now is None:
        return out
    target, tt_hash = tt_now, None
    rest: list[Action] = []
    for a in out:
        if a.kind is Kind.REHASH:
            assert a.task is not None
            target, tt_hash = a.task, a.tt_hash
        elif a.kind is Kind.UPDATE_TT:
            assert a.task is not None
            target = a.task
            rest.append(replace(a, ot_hash=None))  # the OpenTodo write records its own
        else:
            rest.append(a)
    fix = Action(
        Kind.UPDATE_OT,
        tt_id=p.tt_id,
        ot_id=p.ot_id,
        task=target,
        tt_hash=tt_hash,
        completes=target.completed and not damaged.completed,
        note=REPAIR_NOTE,
    )
    return [fix, *rest]


def _plan_rules(
    p: Pair, tt: Snapshot, ot: Snapshot, *, completion_sticks: bool = True
) -> list[Action]:
    tt_task: Task | None
    if p.tt_id in tt.unchanged and p.base is not None:
        tt_task, tt_changed = p.base, False
    else:
        tt_task = tt.tasks.get(p.tt_id)
        tt_changed = tt_task is not None and tt_task.content_hash() != p.tt_hash
    ot_task = ot.tasks.get(p.ot_id)
    ot_changed = ot_task is not None and ot_task.content_hash() != p.ot_hash

    if tt_task is None and ot_task is None:
        return [Action(Kind.FORGET, tt_id=p.tt_id, ot_id=p.ot_id, note="deleted on both sides")]
    if tt_task is None:
        return [
            Action(
                Kind.DELETE_OT,
                tt_id=p.tt_id,
                ot_id=p.ot_id,
                task=ot_task,
                note="deleted in TickTick",
            )
        ]
    if ot_task is None:
        if tt_changed:
            return [
                Action(
                    Kind.CREATE_OT,
                    tt_id=p.tt_id,
                    task=tt_task,
                    tt_hash=tt_task.content_hash(),
                    conflict=True,
                    note="deleted in OpenTodo but changed in TickTick: recreating",
                )
            ]
        return [
            Action(
                Kind.DELETE_TT,
                tt_id=p.tt_id,
                ot_id=p.ot_id,
                task=tt_task,
                note="deleted in OpenTodo",
            )
        ]

    if not tt_changed and not ot_changed:
        return []

    occurrence = _completed_occurrence(p, tt_task, ot_task, ot, tt_changed, ot_changed)
    if occurrence is not None:
        return [occurrence]

    conflict = False
    if tt_changed and ot_changed:
        # TickTick wins, except that a completion on either side sticks.
        sticks = completion_sticks and ot_task.completed
        merged = replace(tt_task, completed=tt_task.completed or sticks)
        conflict = tt_task != ot_task
    elif tt_changed:
        merged = tt_task
    else:
        merged = ot_task

    write_ot, write_tt = ot_task != merged, tt_task != merged
    out: list[Action] = []
    note = "conflict: TickTick wins" if conflict else ""
    if write_ot:
        out.append(
            Action(
                Kind.UPDATE_OT,
                tt_id=p.tt_id,
                ot_id=p.ot_id,
                task=merged,
                conflict=conflict,
                tt_hash=None if write_tt else tt_task.content_hash(),
                completes=merged.completed and not ot_task.completed,
                note=note or "changed in TickTick",
            )
        )
    if write_tt:
        out.append(
            Action(
                Kind.UPDATE_TT,
                tt_id=p.tt_id,
                ot_id=p.ot_id,
                task=merged,
                conflict=conflict and not write_ot,
                ot_hash=None if write_ot else ot_task.content_hash(),
                completes=merged.completed and not tt_task.completed,
                note=note or "changed in OpenTodo",
            )
        )
    if not out:
        out.append(
            Action(
                Kind.REHASH,
                tt_id=p.tt_id,
                ot_id=p.ot_id,
                task=merged,
                tt_hash=tt_task.content_hash(),
                ot_hash=ot_task.content_hash(),
                note="same change on both sides",
            )
        )
    return out


def _completed_occurrence(
    p: Pair, tt_task: Task, ot_task: Task, ot: Snapshot, tt_changed: bool, ot_changed: bool
) -> Action | None:
    """A repeating task completed in OpenTodo: complete it in TickTick, once.

    OpenTodo doesn't mark the task completed: it moves it to its next date and records the
    finished date as a separate, completed occurrence. Pushing the moved date would skip
    TickTick's completion, so this completes the instance TickTick still shows (TickTick
    then advances it itself) and leaves OpenTodo alone. The next pass sees TickTick's new
    date and pushes it, so TickTick decides the next date.
    """
    base = p.base
    if (
        not ot_changed
        or base is None
        or base.due is None
        or base.recurrence is None
        or tt_task.completed
        or ot_task.completed
        or tt_task.due != base.due  # TickTick already moved on (completed there too)
        or ot_task.due == base.due
        or base.due.date not in ot.completed_occurrences.get(p.ot_id, set())
    ):
        return None
    # TickTick wins a conflict; otherwise OpenTodo's other edits go along (at the old date).
    source = tt_task if tt_changed else replace(ot_task, due=base.due)
    done = replace(source, completed=True)
    return Action(
        Kind.UPDATE_TT,
        tt_id=p.tt_id,
        ot_id=p.ot_id,
        task=done,
        ot_hash=ot_task.content_hash(),
        completes=True,
        occurrence=True,
        conflict=tt_changed,
        note=f"repeating task completed in OpenTodo for {base.due.date}",
    )


def destructive_count(actions: Sequence[Action]) -> int:
    return sum(a.destructive for a in actions)


# --- one-time pairing of pre-existing tasks ---------------------------------------------------


@dataclass
class MatchResult:
    matches: list[tuple[str, str]] = field(default_factory=list)
    """(tt_id, ot_id)."""
    tt_unmatched: list[str] = field(default_factory=list)
    ot_unmatched: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, str]] = field(default_factory=list)
    """(project key, title) that occurs more than once on a side."""


def match_existing(pairs: Iterable[Pair], tt: Snapshot, ot: Snapshot) -> MatchResult:
    """Pair unpaired open tasks by (project, exact title); only unique keys are paired."""
    pairs = list(pairs)
    paired_tt = {p.tt_id for p in pairs}
    paired_ot = {p.ot_id for p in pairs}

    def index(snap: Snapshot, paired: set[str]) -> dict[tuple[str, str], list[str]]:
        idx: dict[tuple[str, str], list[str]] = {}
        for task_id, t in sorted(snap.tasks.items()):
            if task_id not in paired and not t.completed:
                idx.setdefault((t.project, t.title), []).append(task_id)
        return idx

    tt_idx, ot_idx = index(tt, paired_tt), index(ot, paired_ot)
    res = MatchResult()
    for key in sorted(tt_idx.keys() | ot_idx.keys()):
        tts, ots = tt_idx.get(key, []), ot_idx.get(key, [])
        if len(tts) == 1 and len(ots) == 1:
            res.matches.append((tts[0], ots[0]))
            continue
        if len(tts) > 1 or len(ots) > 1:
            res.ambiguous.append(key)
        res.tt_unmatched.extend(tts)
        res.ot_unmatched.extend(ots)
    return res


def summarize(actions: Sequence[Action]) -> Counter[Kind]:
    return Counter(a.kind for a in actions)
