# TickTick ↔ OpenTodo Sync

A small Python service that keeps tasks synced between **TickTick** (primary) and a self-hosted **OpenTodo** instance (secondary). It runs on a timer on a Raspberry Pi that also runs Pi-hole.

## Goal

The same tasks should show up in both apps, so I can keep using TickTick day to day while I try out OpenTodo, and switch over cleanly later. Sync goes both ways, but **TickTick is the source of truth and wins every conflict.**

## Environment

- **Host:** a Raspberry Pi (ARM64). Pi-hole already runs on this box and must not be affected.
- **Python:** whatever the host's Python is (expect 3.11+). Use a venv. No Docker needed.
- **TickTick:** cloud, accessed through the TickTick Open API (OAuth2). Developer docs: https://developer.ticktick.com
- **OpenTodo:** self-hosted, public HTTPS URL. Accessed through its REST API with an API token. Docs: https://open-todo.app/docs
- **Scheduling:** systemd service + timer (every 5 minutes), not cron.

## Hard constraints

- **Protect Pi-hole.** Run as a dedicated unprivileged user (`ticksync`). The systemd unit sets `MemoryMax=128M`, `Nice=10`, `CPUQuota=25%`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, with write access only to the state directory.
- **Few dependencies.** Prefer the standard library plus `httpx` (HTTP) and `python-dotenv` if needed. Use stdlib `sqlite3` for state. No ORMs or frameworks.
- **Secrets stay out of git.** Read config from `.env` (gitignored). Store OAuth tokens in the state directory with `0600` permissions. Commit a `.env.example`.
- **Go easy on the SD card.** Write state only when something changed, and use a single SQLite file.
- **Never delete in bulk by accident.** If a run would delete or complete more than N tasks (configurable, default 10), stop, log a warning and exit non-zero.

## Sync rules

TickTick is primary. For each task pair, keep a record of what each side looked like at the last sync (a content hash per side), so the script can tell what changed on which side.

| Situation | Action |
|---|---|
| New task in TickTick | Create in OpenTodo and record the ID pair |
| New task in OpenTodo | Create in TickTick and record the ID pair |
| Changed only in TickTick | Push to OpenTodo |
| Changed only in OpenTodo | Push to TickTick |
| Changed in both since last sync | **TickTick wins**: overwrite OpenTodo and log the conflict |
| Completed in either app | Complete in the other |
| Deleted in TickTick | Delete in OpenTodo |
| Deleted in OpenTodo | Delete in TickTick, unless TickTick also changed it since the last sync (then TickTick wins and the task is recreated in OpenTodo) |

**Loop prevention:** after writing to a side, save that side's new hash right away, so the next run doesn't treat the change it just made as a user edit.

**Projects/lists:** map TickTick lists to OpenTodo projects by name. Keep the mapping in SQLite. Create missing projects on the other side. The TickTick Inbox maps to the OpenTodo Inbox.

### Fields (v1)

Sync: title, notes/description, due date (+ time and time zone), priority (map TickTick's 0/1/3/5 onto OpenTodo's scale and document the mapping), tags, completed status, list/project.

Leave out of v1 (sync these later if wanted):
- **Subtasks/checklist items.** Phase 2.
- **Recurrence rules.** TickTick owns recurring tasks. In v1, sync only the current instance's due date and don't copy recurrence rules. Look at how each API represents recurrence before deciding on phase 2.
- Attachments, comments, reminders, focus/pomodoro data, habits.

## Things to check before writing code

Don't trust these from memory. Check each one against the live API docs or a real response and record what you find in `docs/api-notes.md`:

1. TickTick Open API endpoints for listing projects, getting a project's tasks, creating, updating, completing and deleting tasks. Also the OAuth2 flow (authorization code + refresh) and token lifetime.
2. **How to tell a deleted TickTick task from a completed one.** The project data endpoint may only return open tasks. If a known task disappears, fetch it by ID to check whether it was completed or deleted.
3. Whether the TickTick Inbox shows up in the project list or needs a special ID.
4. Whether the TickTick API gives any "modified since" timestamp or filter. If it doesn't, every run is a full pull plus a hash diff (fine at personal scale).
5. TickTick rate limits.
6. OpenTodo REST API: task/project endpoints, auth header format, field names, how it represents priority, due dates, tags and completion, and whether it has a `modified` timestamp or change feed.

## Suggested layout

```
ticktick-opentodo-sync/
  CLAUDE.md
  README.md                # setup on the Pi, first-run OAuth, ops notes
  pyproject.toml
  .env.example
  src/ticksync/
    __main__.py            # entry point: `python -m ticksync [--dry-run]`
    config.py
    ticktick.py            # API client + OAuth refresh
    opentodo.py            # API client
    models.py              # one normalized Task shape used by both sides
    mapping.py             # field mapping (priority, dates, tags)
    state.py               # SQLite: id pairs, per-side hashes, project map
    engine.py              # the diff + sync rules above
  tests/                   # pytest; mock HTTP (respx); engine tests are pure
  deploy/
    ticksync.service
    ticksync.timer
    install.sh             # create user, venv, state dir, enable timer
  docs/api-notes.md
```

`engine.py` should be pure logic: it takes the previous state plus both current snapshots and returns a list of actions. That way every row of the sync rules table can be unit-tested without network calls.

## Rollout plan

1. **`--dry-run` first.** Log the actions it *would* take without writing anything. Make this the default until I turn it off.
2. **Seed OpenTodo from TickTick** with OpenTodo empty: let the sync create everything rather than matching it against an earlier CSV import. (If OpenTodo already has imported tasks, add a one-time `--match-existing` mode that pairs tasks by project + exact title, and report anything it can't pair.)
3. Run it by hand a few times and check both apps.
4. Turn on the systemd timer.

## Logging and ops

- Log to journald (stdout). One summary line per run: created / updated / completed / deleted / conflicts per side.
- Exit non-zero on errors so `systemctl status` and `journalctl -u ticksync` show failures.
- Add a `--status` command that prints the last run time, counts and any blocked bulk-delete.

## Conventions

Keep the code idiomatic and typed (type hints, `ruff` + `mypy`-clean), and skip beginner explanations. Ask me before adding dependencies.
