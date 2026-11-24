"""Habit sync orchestration: fetch, plan (:mod:`.habits`), apply, record.

Runs after the task pass, on the OpenTodo tasks that pass already fetched.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from . import state as st
from .habits import (
    TT_ACTIVE,
    TT_DONE,
    TT_UNMARKED,
    DayAction,
    DKind,
    HabitAction,
    HabitDef,
    HabitPair,
    HKind,
    Unsupported,
    day_to_stamp,
    habit_from_ot,
    habit_from_tt,
    habit_to_ot,
    habit_to_tt,
    ot_done_days,
    plan_days,
    plan_habits,
    tt_done_days,
    window,
)
from .http import ApiError
from .models import JSON
from .opentodo import PLACEHOLDER

if TYPE_CHECKING:
    from .state import State
    from .sync import PassResult

log = logging.getLogger(__name__)

WINDOW_DAYS = 30
"""Check-ins are compared over the last 30 days (also the first run's backfill)."""
DAILY_RULES = frozenset(
    {"every day", "every monday, tuesday, wednesday, thursday, friday, saturday, sunday"}
)
HABITS_PROJECT = "Habits"
META_PROJECT = "ot_habits_project"
"""Meta key: id of the OpenTodo project new habits go to (kept out of the task sync)."""


class HabitTickTick(Protocol):
    def habits(self) -> list[JSON]: ...
    def habit_sections(self) -> list[JSON]: ...
    def habit_checkins(
        self, habit_ids: list[str], from_stamp: int, to_stamp: int
    ) -> list[JSON]: ...
    def create_habit(self, body: JSON) -> JSON: ...
    def update_habit(self, habit_id: str, body: JSON) -> JSON: ...
    def checkin(self, habit_id: str, body: JSON) -> JSON: ...


class HabitOpenTodo(Protocol):
    def create_project(self, name: str) -> JSON: ...
    def create_task(self, body: JSON, *, idempotency_key: str) -> JSON: ...
    def update_task(self, task_id: str, body: JSON) -> JSON: ...
    def log_habit(self, habit_id: str, day: str, state: str, timezone: str | None) -> JSON: ...


class HabitSync:
    def __init__(
        self,
        tt: HabitTickTick,
        ot: HabitOpenTodo,
        state: State,
        *,
        timezone: str,
        now_ms: Callable[[], int],
        run_id: str,
        max_destructive: int,
    ) -> None:
        self.tt, self.ot, self.state = tt, ot, state
        self.timezone = timezone
        self.now_ms = now_ms
        self.run_id = run_id
        self.max_destructive = max_destructive
        self._noted: set[str] = set()

    def run(
        self,
        ot_habits: dict[str, JSON],
        ot_records: Iterable[JSON],
        ot_project_ids: set[str],
        res: PassResult,
        *,
        dry_run: bool,
        allow_bulk: bool,
    ) -> None:
        now = datetime.fromtimestamp(self.now_ms() / 1000, UTC)
        days = window(now.astimezone(ZoneInfo(self.timezone)).date(), WINDOW_DAYS)

        tt_raw = {
            str(h["id"]): h
            for h in self.tt.habits()
            if h.get("id") and int(h.get("status") or 0) == TT_ACTIVE
        }
        sections = self.tt.habit_sections()
        section_names = {str(s.get("id")): str(s.get("name") or "") for s in sections}
        section_ids = {name: sid for sid, name in section_names.items()}
        tt_defs: dict[str, HabitDef] = {}
        for hid, raw in tt_raw.items():
            try:
                tt_defs[hid] = habit_from_tt(raw, section_names)
            except Unsupported as exc:
                self._note(f"tt:{hid}", f"TickTick habit {raw.get('name')!r} not synced: {exc}")
        pairs = st.habit_pairs(self.state)
        pair_by_ot = {p.ot_id: p for p in pairs}
        ot_defs: dict[str, HabitDef] = {}
        damaged: set[str] = set()
        for oid, raw in ot_habits.items():
            h = habit_from_ot(raw)
            if raw.get("title") == PLACEHOLDER:
                # An open browser saved the create placeholder over the name (see the task
                # sync's Snapshot.damaged): never an edit; the name from the last sync stands in.
                p = pair_by_ot.get(oid)
                if p is None or p.base is None:
                    self._note(
                        f"ot:{oid}",
                        f"OpenTodo habit {oid} holds the create placeholder "
                        "and isn't paired; delete it in OpenTodo",
                    )
                    continue
                h = replace(h, name=p.base.name)
                damaged.add(oid)
            ot_defs[oid] = h
        ot_due = {
            oid: str((raw.get("due") or {}).get("date") or "") for oid, raw in ot_habits.items()
        }

        tt_done = tt_done_days(
            self.tt.habit_checkins(sorted(tt_defs), day_to_stamp(days[0]), day_to_stamp(days[-1]))
        )
        ot_done = ot_done_days(r for r in ot_records if str(r.get("series_id")) in ot_defs)

        day_pairs = [(p.tt_id, p.ot_id) for p in pairs if p.tt_id in tt_defs and p.ot_id in ot_defs]
        names = {
            p.tt_id: (tt_defs.get(p.tt_id) or ot_defs[p.ot_id]).name
            for p in pairs
            if p.tt_id in tt_defs or p.ot_id in ot_defs
        }

        for a in self._plan(pairs, tt_defs, ot_defs, damaged):
            desc = f"{a.kind.value} habit {a.habit.name!r}: {a.note}"
            if dry_run:
                if a.kind is not HKind.REHASH:
                    log.info("DRY-RUN would %s", desc)
                pair = self._pseudo_pair(a)
                _count_habit(a, res)
            else:
                try:
                    pair = self._apply_habit(
                        a, tt_raw, tt_done, days, section_names, section_ids, ot_due, ot_project_ids
                    )
                except Exception as exc:
                    res.errors += 1
                    log.error("failed to %s: %s", desc, exc)  # noqa: TRY400
                    log.debug("traceback", exc_info=True)
                    continue
                if a.kind is not HKind.REHASH:
                    (log.warning if a.conflict else log.info)("%s", desc)
                _count_habit(a, res)
            if a.kind in (HKind.CREATE_OT, HKind.CREATE_TT) and pair is not None:
                day_pairs.append(pair)
                names[pair[0]] = a.habit.name
                ot_defs.setdefault(pair[1], a.habit)

        base = st.habit_days(self.state)
        actions = plan_days(day_pairs, days, tt_done, ot_done, base)
        clears = sum(a.kind is DKind.CLEAR_TT for a in actions)
        if clears > self.max_destructive and not allow_bulk:
            res.errors += 1
            log.warning(
                "habits: %d check-ins would be cleared in TickTick (more than %d); none written. "
                "Review, then run once with --allow-bulk.",
                clears,
                self.max_destructive,
            )
            return
        self._apply_days(actions, names, ot_due, res, dry_run=dry_run)
        if not dry_run:
            st.prune_habit_days(self.state, days[0])
        self._back_to_today(
            day_pairs, ot_defs, ot_due, ot_records, tt_done, actions, days[-1], dry_run=dry_run
        )

    def _plan(
        self,
        pairs: list[HabitPair],
        tt_defs: dict[str, HabitDef],
        ot_defs: dict[str, HabitDef],
        damaged: set[str],
    ) -> list[HabitAction]:
        """:func:`plan_habits`, plus a rewrite of every OpenTodo habit holding the placeholder."""
        actions = plan_habits(pairs, tt_defs, ot_defs)
        fixes = {
            p.ot_id: HabitAction(
                HKind.UPDATE_OT,
                tt_defs[p.tt_id],
                p.tt_id,
                p.ot_id,
                note="OpenTodo held the create placeholder: rewritten",
            )
            for p in pairs
            if p.ot_id in damaged and p.tt_id in tt_defs
        }
        return [a for a in actions if a.ot_id not in fixes] + list(fixes.values())

    def _back_to_today(
        self,
        day_pairs: list[tuple[str, str]],
        ot_defs: dict[str, HabitDef],
        ot_due: dict[str, str],
        ot_records: Iterable[JSON],
        tt_done: set[tuple[str, str]],
        actions: list[DayAction],
        today: str,
        *,
        dry_run: bool,
    ) -> None:
        """Keep a daily OpenTodo habit on today while today is still open on both sides.

        Logging an overdue day completes OpenTodo's current occurrence, which jumps the habit
        past today; it would then not be offered today. Skipped or done today: left alone.
        """
        logged = {
            (str(r.get("series_id")), str(r.get("occurrence_date")))
            for r in ot_records
            if r.get("deleted_at") is None
        }
        # Today's state once this pass's writes are in (both sides then agree).
        final = {a.tt_id: a.done for a in actions if a.day == today}
        for tt_id, ot_id in day_pairs:
            h = ot_defs.get(ot_id)
            done_today = final.get(tt_id, (tt_id, today) in tt_done)
            skipped_today = (ot_id, today) in logged and final.get(tt_id) is not False
            if (
                h is None
                or h.rule not in DAILY_RULES
                or not ot_due.get(ot_id, "") > today
                or done_today
                or skipped_today
            ):
                continue
            if dry_run:
                log.info("DRY-RUN would move OpenTodo habit %r back to today", h.name)
                continue
            try:
                self.ot.update_task(ot_id, {"due": {"date": today}})
            except Exception as exc:
                log.error("could not move OpenTodo habit %s back to today: %s", ot_id, exc)  # noqa: TRY400
                continue
            ot_due[ot_id] = today
            log.info("moved OpenTodo habit %r back to today", h.name)

    # --- definitions ------------------------------------------------------------------

    def _pseudo_pair(self, a: HabitAction) -> tuple[str, str] | None:
        if a.kind is HKind.CREATE_OT and a.tt_id:
            return a.tt_id, f"new:{a.tt_id}"
        if a.kind is HKind.CREATE_TT and a.ot_id:
            return f"new:{a.ot_id}", a.ot_id
        return None

    def _apply_habit(
        self,
        a: HabitAction,
        tt_raw: dict[str, JSON],
        tt_done: set[tuple[str, str]],
        days: list[str],
        section_names: dict[str, str],
        section_ids: dict[str, str],
        ot_due: dict[str, str],
        ot_project_ids: set[str],
    ) -> tuple[str, str] | None:
        h = a.habit
        if a.kind is HKind.CREATE_OT:
            assert a.tt_id
            # OpenTodo takes check-ins only from a habit's first day on: start it on the first
            # day there is one to copy (see _apply_days for how it then catches up).
            first = min((d for t, d in tt_done if t == a.tt_id and d in days), default=days[-1])
            body = habit_to_ot(h) | {
                "project_id": self._habits_project(ot_project_ids),
                "due": {"date": first},
            }
            resp = self.ot.create_task(
                body, idempotency_key=f"ticktick-opentodo-sync-habit-{a.tt_id}-{self.run_id}"
            )
            ot_id = str(resp["id"])
            ot_due[ot_id] = first
            self._put(a.tt_id, ot_id, h.content_hash(), habit_from_ot(resp).content_hash(), h)
            return a.tt_id, ot_id
        if a.kind is HKind.CREATE_TT:
            assert a.ot_id
            resp = self.tt.create_habit(habit_to_tt(h, None, section_ids))
            tt_id = str(resp["id"])
            self._put(tt_id, a.ot_id, self._tt_hash(resp, section_names, h), h.content_hash(), h)
            return tt_id, a.ot_id
        assert a.tt_id and a.ot_id
        if a.kind is HKind.UPDATE_OT:
            resp = self.ot.update_task(a.ot_id, habit_to_ot(h))
            self._put(a.tt_id, a.ot_id, h.content_hash(), habit_from_ot(resp).content_hash(), h)
        elif a.kind is HKind.UPDATE_TT:
            resp = self.tt.update_habit(a.tt_id, habit_to_tt(h, tt_raw.get(a.tt_id), section_ids))
            self._put(a.tt_id, a.ot_id, self._tt_hash(resp, section_names, h), h.content_hash(), h)
        else:  # REHASH
            self._put(a.tt_id, a.ot_id, h.content_hash(), h.content_hash(), h)
        return None

    @staticmethod
    def _tt_hash(resp: JSON, section_names: dict[str, str], fallback: HabitDef) -> str:
        try:
            return habit_from_tt(resp, section_names).content_hash() if resp.get("id") else ""
        except Unsupported:
            return fallback.content_hash()

    def _put(self, tt_id: str, ot_id: str, tt_hash: str, ot_hash: str, base: HabitDef) -> None:
        st.put_habit_pair(
            self.state, HabitPair(tt_id, ot_id, tt_hash or base.content_hash(), ot_hash, base)
        )

    def _habits_project(self, ot_project_ids: set[str]) -> str:
        pid = self.state.get_meta(META_PROJECT)
        if pid and pid in ot_project_ids:
            return pid
        created = self.ot.create_project(HABITS_PROJECT)
        pid = str(created["id"])
        self.state.set_meta(META_PROJECT, pid)
        ot_project_ids.add(pid)
        log.info("created OpenTodo project %r for habits", HABITS_PROJECT)
        return pid

    # --- days -------------------------------------------------------------------------

    def _apply_days(
        self,
        actions: list[DayAction],
        names: dict[str, str],
        ot_due: dict[str, str],
        res: PassResult,
        *,
        dry_run: bool,
    ) -> None:
        done: dict[tuple[str, DKind], list[str]] = {}
        for a in actions:
            if a.kind is DKind.RECORD:
                if not dry_run:
                    st.set_habit_day(self.state, a.tt_id, a.day, a.done)
                continue
            if not dry_run:
                try:
                    self._apply_day(a, ot_due)
                except Exception as exc:
                    res.errors += 1
                    log.error(  # noqa: TRY400
                        "failed to %s habit %r on %s: %s",
                        a.kind.value,
                        names.get(a.tt_id, a.tt_id),
                        a.day,
                        exc,
                    )
                    log.debug("traceback", exc_info=True)
                    continue
                st.set_habit_day(self.state, a.tt_id, a.day, a.done)
            done.setdefault((a.tt_id, a.kind), []).append(a.day)
            side = res.habits_ot if a.kind in (DKind.CHECK_OT, DKind.CLEAR_OT) else res.habits_tt
            if a.done:
                side.completed += 1
            else:
                side.deleted += 1
        prefix = "DRY-RUN would " if dry_run else ""
        for (tt_id, kind), ds in done.items():
            where = "OpenTodo" if kind in (DKind.CHECK_OT, DKind.CLEAR_OT) else "TickTick"
            verb = "check in" if kind in (DKind.CHECK_OT, DKind.CHECK_TT) else "clear"
            span = ds[0] if len(ds) == 1 else f"{len(ds)} days, {ds[0]} .. {ds[-1]}"
            log.info("%s%s habit %r in %s: %s", prefix, verb, names.get(tt_id, tt_id), where, span)

    def _apply_day(self, a: DayAction, ot_due: dict[str, str]) -> None:
        if a.kind is DKind.CHECK_OT:
            due = ot_due.get(a.ot_id, "")
            if due and a.day > due:
                # The habit sits on an earlier (missed) day; OpenTodo only completes the current
                # day, so move it to this one first. The days skipped read as missed.
                self.ot.update_task(a.ot_id, {"due": {"date": a.day}})
            try:
                resp = self.ot.log_habit(a.ot_id, a.day, "done", self.timezone)
            except ApiError as exc:
                if exc.status != 400:
                    raise
                log.warning("OpenTodo won't take habit day %s (%s); left as is", a.day, exc)
                return
            current = (resp.get("habit") or {}).get("current_date")
            if a.day >= due or not due:
                ot_due[a.ot_id] = str(current or _next_day(a.day))
        elif a.kind is DKind.CLEAR_OT:
            self.ot.log_habit(a.ot_id, a.day, "clear", self.timezone)
        else:
            at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
            self.tt.checkin(
                a.tt_id,
                {
                    "stamp": day_to_stamp(a.day),
                    "status": TT_DONE if a.done else TT_UNMARKED,
                    "value": 1.0 if a.done else 0.0,
                    "goal": 1.0,
                    "time": at,
                    "opTime": at,
                },
            )

    def _note(self, key: str, message: str) -> None:
        """Log once per process (the loop runs every few minutes)."""
        if key not in self._noted:
            self._noted.add(key)
            log.warning("%s", message)


def _next_day(day: str) -> str:
    return date.fromordinal(date.fromisoformat(day).toordinal() + 1).isoformat()


def _count_habit(a: HabitAction, res: PassResult) -> None:
    res.conflicts += a.conflict
    if a.kind is HKind.CREATE_OT:
        res.habits_ot.created += 1
    elif a.kind is HKind.CREATE_TT:
        res.habits_tt.created += 1
    elif a.kind is HKind.UPDATE_OT:
        res.habits_ot.updated += 1
    elif a.kind is HKind.UPDATE_TT:
        res.habits_tt.updated += 1
