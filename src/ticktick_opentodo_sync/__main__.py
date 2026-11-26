"""CLI entry point.

    ticktick-opentodo-sync [--dry-run|--apply] [--loop [SECONDS]] [--allow-bulk]
    ticktick-opentodo-sync --match-existing [--dry-run|--apply]
    ticktick-opentodo-sync --status | --health | --recurrence-report

Exit codes: 0 ok, 1 errors, 2 bad config, 3 blocked (bulk guard / first-sync guard / lock).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import resource
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from . import __version__
from .config import Config, ConfigError, load
from .http import Http
from .opentodo import OpenTodo
from .state import State, file_lock
from .sync import PassResult, Syncer, deletes_in_last_day
from .ticktick import TickTick

log = logging.getLogger("ticktick_opentodo_sync")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_BLOCKED = 0, 1, 2, 3


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ticktick-opentodo-sync", description="TickTick <-> OpenTodo task sync"
    )
    p.add_argument("--version", action="version", version=f"ticktick-opentodo-sync {__version__}")
    p.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="read settings from this file too (default: ./.env if present)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="log what would change, write nothing (default: TICKTICK_OPENTODO_SYNC_DRY_RUN)",
    )
    mode.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="write changes, overriding TICKTICK_OPENTODO_SYNC_DRY_RUN=true",
    )
    p.add_argument(
        "--allow-bulk",
        action="store_true",
        help="let this run exceed TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE",
    )
    p.add_argument(
        "--loop",
        nargs="?",
        type=int,
        const=-1,
        metavar="SECONDS",
        help="run forever, one pass every SECONDS (default: TICKTICK_OPENTODO_SYNC_INTERVAL)",
    )
    cmd = p.add_mutually_exclusive_group()
    cmd.add_argument(
        "--match-existing",
        action="store_true",
        help="one-time: pair existing tasks by project + exact title, then exit",
    )
    cmd.add_argument(
        "--recurrence-report",
        action="store_true",
        help="report how TickTick repeat rules would translate to OpenTodo; writes nothing",
    )
    cmd.add_argument("--status", action="store_true", help="print state summary and exit")
    cmd.add_argument(
        "--health",
        action="store_true",
        help="exit 0 if the loop's last pass was fine and recent (HEALTHCHECK)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def _setup_logging(level: str) -> None:
    # Docker/journald add their own timestamps; keep them for interactive use.
    fmt = (
        "%(levelname)s %(message)s"
        if not sys.stdout.isatty()
        else ("%(asctime)s %(levelname)s %(message)s")
    )
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _build(cfg: Config) -> tuple[Syncer, State, list[Http]]:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    tt_http = Http(
        "TickTick",
        cfg.ticktick_base_url,
        cfg.ticktick_token,
        min_interval=cfg.http_min_interval,
        timeout=cfg.http_timeout,
    )
    ot_http = Http(
        "OpenTodo",
        cfg.opentodo_url,
        cfg.opentodo_token,
        min_interval=cfg.http_min_interval,
        timeout=cfg.http_timeout,
    )
    state = State(cfg.db_path)
    syncer = Syncer(
        TickTick(tt_http),
        OpenTodo(
            ot_http,
            passphrase=cfg.opentodo_passphrase,
            recovery_key=cfg.opentodo_recovery_key,
        ),
        state,
        timezone=cfg.timezone,
        max_destructive=cfg.max_destructive,
        max_deletes_per_day=cfg.max_deletes_per_day,
    )
    return syncer, state, [tt_http, ot_http]


MEMORY_LIMIT_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
"""Where the container's memory limit is: cgroup v2, then v1."""
MEMORY_WARN_SHARE = 0.8


def peak_memory_mb() -> int:
    """This process's peak resident memory so far (Linux reports it in KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def memory_limit_mb(files: Sequence[str] = MEMORY_LIMIT_FILES) -> int | None:
    """The memory limit of the container this runs in, or None when there's none to read.

    Every pass holds OpenTodo's whole task list, which only grows. Showing the peak next to
    the limit (compose sets 128 MB) shows the headroom before an OOM kill.
    """
    for name in files:
        try:
            text = Path(name).read_text().strip()
        except OSError:
            continue
        # "max" (v2) or a value near 2**63 (v1) means no limit.
        return int(text) // (1024 * 1024) if text.isdigit() and int(text) < 1 << 50 else None
    return None


def _write_heartbeat(cfg: Config, payload: dict[str, Any]) -> None:
    path = cfg.heartbeat_path
    if path is None:
        return
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True))
        tmp.replace(path)
    except OSError as exc:
        log.debug("cannot write heartbeat %s: %s", path, exc)


def _one_pass(cfg: Config, syncer: Syncer, args: argparse.Namespace, dry_run: bool) -> int:
    started = time.time()
    try:
        with file_lock(cfg.lock_path, timeout=120):
            result: PassResult = syncer.run(dry_run=dry_run, allow_bulk=args.allow_bulk)
    except TimeoutError as exc:
        log.warning("%s; skipping this pass", exc)
        return EXIT_BLOCKED
    except Exception as exc:
        error = str(exc)
        if isinstance(exc, sqlite3.Error):
            # Raised before the pass wrote to either app, or right after a write it couldn't
            # record. Nothing more is written to either app until the state is writable.
            error = f"state database {cfg.db_path}: {exc}"
        log.error("sync pass failed: %s", error)  # noqa: TRY400
        log.debug("traceback", exc_info=True)
        _write_heartbeat(
            cfg,
            {
                "at": time.time(),
                "ok": False,
                "error": error,
                "interval": cfg.interval,
                "dry_run": dry_run,
            },
        )
        return EXIT_ERROR
    peak, limit = peak_memory_mb(), memory_limit_mb()
    memory = f"memory peak {peak} MB" + (f" of {limit} MB" if limit else "")
    log.info("%s (%.1fs, %s)", result.summary(), time.time() - started, memory)
    if limit and peak > limit * MEMORY_WARN_SHARE:
        log.warning(
            "memory peak %d MB is close to the %d MB limit: OpenTodo's task list grows "
            "(%d read); see README, Running next to Pi-hole",
            peak,
            limit,
            result.ot_tasks_read,
        )
    _write_heartbeat(
        cfg, {"at": time.time(), "interval": cfg.interval, "peak_mb": peak} | result.to_json()
    )
    if result.blocked or result.refused:
        return EXIT_BLOCKED
    return EXIT_ERROR if result.errors else EXIT_OK


def _loop(cfg: Config, syncer: Syncer, args: argparse.Namespace, dry_run: bool) -> int:
    interval = cfg.interval if args.loop is None or args.loop < 0 else max(args.loop, 60)
    cfg = dataclasses.replace(cfg, interval=interval)
    stop = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s; stopping after the current pass", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    log.info(
        "ticktick-opentodo-sync %s: loop every %ds (%s)",
        __version__,
        interval,
        "dry run" if dry_run else "APPLYING changes",
    )
    while not stop.is_set():
        _one_pass(cfg, syncer, args, dry_run)
        args.allow_bulk = False  # --allow-bulk covers the first pass only
        stop.wait(interval)
    return EXIT_OK


def _fmt_ms(ms: object) -> str:
    if not isinstance(ms, int | float):
        return "never"
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def _status(cfg: Config) -> int:
    if not cfg.db_path.exists():
        print(f"no state yet at {cfg.db_path}")
        return EXIT_OK
    state = State(cfg.db_path)
    try:
        print(f"state:            {cfg.db_path}")
        print(f"task pairs:       {state.pair_count()}")
        print(f"project pairs:    {len(state.project_pairs())}")
        print(f"tt inbox id:      {state.get_meta('tt_inbox_id') or 'unknown'}")
        print(f"dry-run default:  {cfg.dry_run}")
        hb = _read_heartbeat(cfg)
        if hb:
            when = datetime.fromtimestamp(hb.get("at", 0), UTC).isoformat(timespec="seconds")
            memory = f" memory peak {hb['peak_mb']} MB" if "peak_mb" in hb else ""
            print(f"last pass:        {when} ok={hb.get('ok')} dry_run={hb.get('dry_run')}{memory}")
            if hb.get("error"):
                print(f"  error:          {hb['error']}")
        last = state.get_meta_json("last_change")
        if last:
            print(f"last change:      {_fmt_ms(last.get('at'))}")
            for side, name in (("tt", "TickTick"), ("ot", "OpenTodo")):
                c = last.get(side, {})
                print(f"  {name + ':':<15} " + " ".join(f"{k}={v}" for k, v in c.items()))
            print(f"  conflicts={last.get('conflicts')} errors={last.get('errors')}")
        else:
            print("last change:      never")
        recent = deletes_in_last_day(state, int(time.time() * 1000))
        print(f"deletes (24 h):   {recent} of {cfg.max_deletes_per_day}")
        blocked = state.get_meta_json("blocked")
        if blocked:
            print(f"BLOCKED since {_fmt_ms(blocked.get('at'))}: {blocked.get('reason')}")
            return EXIT_BLOCKED
        print("blocked:          no")
        return EXIT_OK
    finally:
        state.close()


def _read_heartbeat(cfg: Config) -> dict[str, Any] | None:
    path = cfg.heartbeat_path
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _health(cfg: Config) -> int:
    hb = _read_heartbeat(cfg)
    if hb is None:
        print("no heartbeat yet")
        return EXIT_ERROR
    age = time.time() - float(hb.get("at", 0))
    max_age = 3 * float(hb.get("interval", cfg.interval)) + 120
    problems = []
    if age > max_age:
        problems.append(f"last pass {age:.0f}s ago")
    if not hb.get("ok"):
        problems.append(
            hb.get("error")
            or hb.get("blocked")
            or hb.get("refused")
            or f"{hb.get('errors')} error(s)"
        )
    if problems:
        print("unhealthy: " + "; ".join(str(p) for p in problems))
        return EXIT_ERROR
    print(f"healthy: last pass {age:.0f}s ago")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    # What the sync writes (state with decrypted task content, its lock, the heartbeat) is
    # for its own user only, whatever umask cron, systemd or a shell hands it.
    os.umask(0o077)
    try:
        cfg = load(env_file=args.env_file)
    except (ConfigError, OSError) as exc:
        if args.health or args.status:
            # Health/status must work even without secrets: fall back to paths only.
            cfg = Config(
                ticktick_token="",
                opentodo_url="",
                opentodo_token="",
                state_dir=Path(os.environ.get("TICKTICK_OPENTODO_SYNC_STATE_DIR", "state")),
                run_dir=Path(os.environ["TICKTICK_OPENTODO_SYNC_RUN_DIR"])
                if os.environ.get("TICKTICK_OPENTODO_SYNC_RUN_DIR")
                else None,
            )
        else:
            print(f"ticktick-opentodo-sync: config error: {exc}", file=sys.stderr)
            return EXIT_CONFIG

    if args.health:
        return _health(cfg)
    _setup_logging("DEBUG" if args.verbose else cfg.log_level)
    for warning in cfg.warnings:
        log.warning("config: %s", warning)
    if args.status:
        return _status(cfg)

    dry_run = cfg.dry_run if args.dry_run is None else args.dry_run
    syncer, state, clients = _build(cfg)
    try:
        if args.match_existing:
            with file_lock(cfg.lock_path, timeout=120):
                syncer.match_existing(dry_run=dry_run)
            return EXIT_OK
        if args.recurrence_report:
            for line in syncer.recurrence_report().lines():
                log.info("%s", line)
            return EXIT_OK
        if args.loop is not None:
            return _loop(cfg, syncer, args, dry_run)
        return _one_pass(cfg, syncer, args, dry_run)
    finally:
        state.close()
        for c in clients:
            c.close()


if __name__ == "__main__":
    sys.exit(main())
