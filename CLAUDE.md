# TickTick ↔ OpenTodo Sync

A small Python service that keeps tasks synced between **TickTick** (primary) and a self-hosted **OpenTodo** instance (secondary). It runs as a Docker Compose service on a Raspberry Pi that also runs Pi-hole.

## Goal

The same tasks should show up in both apps, so I can keep using TickTick day to day while I try out OpenTodo, and switch over cleanly later. Sync goes both ways, but **TickTick is the source of truth and wins every conflict.**

## Environment

- **Host:** Raspberry Pi 4 Model B, 4GB RAM, DietPi (Debian-based, ARM64). Pi-hole already runs on this box and must not be affected.
- **Runtime:** Docker + Docker Compose. The image is built **on the Pi** from a git checkout (`docker compose up -d --build`), with no registry. Base image: `python:3.13-slim` (multi-arch, so the same Dockerfile builds on amd64 for dev).
- **Dev machine:** Python 3.10 without `ensurepip`, so no local venv. Lint, type-check and test inside Docker: `docker build --target test .`
- **TickTick:** cloud, accessed through the TickTick Open API with a **personal API token** (TickTick → Settings → Account → API Token), not OAuth2. Developer docs: <https://developer.ticktick.com> (the readable source is <https://developer.ticktick.com/docs/openapi.md>)
- **OpenTodo:** self-hosted on PikaPods, public HTTPS URL. Accessed through its REST API with an API token. Docs: <https://open-todo.app/docs>
- **Scheduling:** an in-container loop (`ticktick-opentodo-sync --loop 300`) under `restart: unless-stopped`. No systemd units, no cron.

## Hard constraints

- **Protect Pi-hole.** The compose service runs as a non-root UID, with `mem_limit: 128m`, `cpus: 0.25`, `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, a tmpfs for `/tmp` and `/run/ticktick-opentodo-sync`, and the `/data` state volume as its only writable path. It needs no published ports and no host networking.
- **Few dependencies.** Prefer the standard library plus `httpx` (HTTP) and `cryptography` (OpenTodo E2EE). Use stdlib `sqlite3` for state. No ORMs or frameworks.
- **Secrets stay out of git.** Read config from `.env` (gitignored) next to `compose.yaml`, loaded with `env_file:`. Commit a `.env.example`.
- **Go easy on the SD card.** Write state only when something changed, and use a single SQLite file. The per-pass heartbeat goes to tmpfs, not the SD card. Docker logs use `json-file` with small rotation.
- **Never delete in bulk by accident.** If a pass would delete or complete more than N tasks (configurable, default 10), it stops, logs a warning and records the block. In one-shot mode it exits non-zero. In loop mode the container keeps running and reports unhealthy until a run with `--allow-bulk` clears the block.

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

**Projects/lists:** map TickTick lists to OpenTodo projects by name. Keep the mapping in SQLite. Create missing projects on the other side. The TickTick Inbox maps to the OpenTodo Inbox (`project_id: null`).

### Fields (v1)

Sync: title, notes/description, due date (+ time and time zone), priority (map TickTick's 0/1/3/5 onto OpenTodo's scale and document the mapping), tags, completed status, list/project, and (phase 2, 2026-11-22) recurrence rules, subtasks and checklist items, and (phase 3, 2026-11-22) reminders.

Leave out (sync these later if wanted):
- Attachments, comments, focus/pomodoro data, habits.

## API facts

Checked against the live docs on 2026-11-21. Details are in `docs/api-notes.md`. Update that file, not memory, when something turns out different in a real response.

## Layout

```text
ticktick-opentodo-sync/
  CLAUDE.md
  README.md                # setup on the Pi, first-run, ops notes
  Dockerfile               # stages: build, test (ruff+mypy+pytest), runtime
  compose.yaml
  pyproject.toml
  .env.example
  src/ticktick_opentodo_sync/
    __main__.py            # CLI: `ticktick-opentodo-sync [--dry-run|--apply] [--loop N] [--status] ...`
    config.py
    http.py                # shared httpx client: throttle + retry on 429/5xx
    ticktick.py            # TickTick API client
    opentodo.py            # OpenTodo API client
    models.py              # one normalized Task shape used by both sides
    mapping.py             # field mapping (priority, dates, tags) + project reconciliation
    state.py               # SQLite: id pairs, per-side hashes, project map, meta
    engine.py              # pure diff + sync rules above
    sync.py                # orchestration: fetch, plan, guard, apply, record
  tests/                   # pytest; mock HTTP (respx); engine tests are pure
  docs/api-notes.md
```

`engine.py` is pure logic: it takes the previous state plus both current snapshots and returns a list of actions. That way every row of the sync rules table can be unit-tested without network calls.

## Rollout plan

1. **`--dry-run` first.** Log the actions it *would* take without writing anything. This is the default (`TICKTICK_OPENTODO_SYNC_DRY_RUN=true`) until I turn it off.
2. **Seed OpenTodo from TickTick** with OpenTodo empty: let the sync create everything. (Fallback if OpenTodo ever holds tasks before the first sync: the optional one-time `--match-existing` mode pairs tasks by project + exact title and reports anything it can't pair.)
3. Run it by hand a few times (`docker compose run --rm ticktick-opentodo-sync --apply`) and check both apps.
4. Set `TICKTICK_OPENTODO_SYNC_DRY_RUN=false` and `docker compose up -d`.
5. **Once the sync has run cleanly for a while, purge OpenTodo's plaintext history** (`POST /api/v1/encryption/purge-history`, or the button in OpenTodo's encryption settings). It still holds unencrypted copies of every task from the 2026-11-21 seed run. Don't purge before then: that history was the recovery source for the incident in the decisions log.

## To review later

Open items Jesse parked on 2026-11-22. Raise them when relevant; don't act on them unasked.

- **Purge OpenTodo's plaintext history** (rollout step 5): once the sync has run cleanly for a while.
- **Subtasks of repeating tasks in OpenTodo aren't unticked when the task repeats.** OpenTodo's app (`web/src/lib/ops.ts` `completeTask`, checked at commit `229c34b`) records the occurrence and moves the task on without touching its subtasks, and has no setting for it. While TickTick is primary this doesn't matter: TickTick unticks its checklist and the sync copies that over. Before dropping TickTick, pick one:
  - ask OpenTodo for the feature ("untick subtasks when a repeating task moves to its next occurrence"), or
  - make the sync untick a task's subtasks when it sees a completed OpenTodo occurrence (OpenTodo-only mode).

## Decisions log

Answers to questions asked during the build (2026-11-21). Treat as settled.

- **TickTick auth:** personal API token in `TICKTICK_TOKEN`. No OAuth flow, no refresh logic. (TickTick's OAuth docs only describe the `authorization_code` grant, with no refresh grant and no documented lifetime.)
- **Dependencies:** runtime `httpx`, plus `cryptography` (approved 2026-11-21, for OpenTodo E2EE: the stdlib has no AES). No `python-dotenv`: compose's `env_file:` loads `.env`, and `config.py` has a small stdlib parser for runs outside Docker. Dev extra: `pytest`, `respx`, `ruff`, `mypy`.
- **Docker:** deployed with Docker Compose on the Pi, built on the Pi, scheduled by an in-container loop (not systemd, not cron). This replaces the earlier systemd/venv plan.
- **Naming:** everything is `ticktick-opentodo-sync`: CLI command, Docker image, Compose service, container, volume (`ticktick-opentodo-sync-data`), Unix user, paths and files. Where hyphens aren't allowed, the import package is `ticktick_opentodo_sync` and the env var prefix is `TICKTICK_OPENTODO_SYNC_`. Don't reintroduce the old short name `ticksync`.
- **OpenTodo has end-to-end encryption ON** (learned 2026-11-21, after it broke a sync). Every REST read returns task `title`/`description` and project/label `name` as `enc:v1:<base64url(nonce|AES-256-GCM|tag)>`; the server never decrypts. Run 2 read that ciphertext as "changed in OpenTodo" and pushed it into 66 TickTick tasks; they were restored from `GET /api/v1/tasks/{id}/history`.
  - **Decision: the sync decrypts and encrypts like OpenTodo's own apps** (`e2ee.py`, inside `opentodo.py` so everything above it sees plaintext). Secret in `.env`: `OPENTODO_ENCRYPTION_RECOVERY_KEY` or `OPENTODO_ENCRYPTION_PASSPHRASE`. Accepted trade-off: someone with access to the Pi can read OpenTodo; PikaPods still can't. Nothing plaintext is written to OpenTodo: creates send a placeholder, then PATCH the ciphertext bound to the new id (REST creates can't take a client id).
  - **Placeholder race (2026-11-22):** a browser open on OpenTodo can save its encrypted copy of the create placeholder *after* the sync's PATCH (seen 0.4 s later), wiping the content. The placeholder is never an OpenTodo edit: for a paired task or subtask, the engine uses the last-synced title/notes in its place, applies the normal rules, and rewrites OpenTodo (TickTick wins). An unpaired placeholder task (interrupted create) is left alone with a warning, and the TickTick client refuses any write containing the placeholder.
  - Safety: without a usable key the pass refuses to run; values that don't decrypt keep their task/project/label out of scope; the TickTick client refuses any write containing `enc:vN:`.
  - The 66 state rows with ciphertext `base` healed on their own: after the TickTick restore both sides held the same content, so the engine only re-hashed them (no writes).
  - Verified 2026-11-22: dry run reviewed (1 action), `--apply` ran clean, the follow-up pass wrote nothing, and no state row holds ciphertext. `--apply` is no longer on hold.
- **TickTick "Won't Do"** (status `-1`): treated as completed. OpenTodo has no equivalent state.
- **OpenTodo starts empty; seed everything from TickTick.** (Updated 2026-11-21: the TickTick backup was too big for OpenTodo's importer.) `--match-existing` stays as an optional fallback. Safety net: a real run refuses to start with an empty pair table while OpenTodo has open tasks (e.g. leftovers from the failed import). Delete them, or run `--match-existing`, first.
- **Tags ↔ labels:** OpenTodo has no tags. It has labels, referenced by ID. They are matched by lowercase name, and missing labels are created.
- **Priority mapping:** TickTick `0/1/3/5` (none/low/medium/high) ↔ OpenTodo `4/3/2/1` (1 is highest, 4 is the default).
- **Conflicts:** TickTick wins every field except completion. A completion made on either side is never undone by a conflict ("Completed in either app" takes priority).
- **Subtasks (2026-11-22, replaces "out of scope in v1").** One level deep on both sides; deeper levels are ignored.
  - TickTick **checklist items** ↔ OpenTodo **subtasks**, including checked items (as completed subtasks). Items have their own pair table (`item_pairs`; `tt_project` = TickTick parent id) and sync title and done state only. Order is copied on create (`sortOrder` → `position`), not afterwards. Items sync only while the parent is open and paired on both sides.
  - TickTick **child tasks** (`parentId`) ↔ OpenTodo subtasks as full tasks (`Task.parent`).
  - A **new OpenTodo subtask** becomes a checklist item under a TickTick CHECKLIST task, otherwise a TickTick child task (Jesse's choice: each app keeps its natural form). Subtasks of a brand-new parent follow one pass later.
  - Item conflicts: TickTick wins outright, done state included (a repeating TickTick checklist resets its items, which must not be re-ticked from OpenTodo). After a repeating task is completed in OpenTodo, its items are resynced from TickTick on the next pass.
  - The bulk guard counts item deletions in TickTick, not in OpenTodo (OpenTodo's copies can be recreated).
- **Reminders (phase 3, 2026-11-22).** `Task.reminders` holds offsets in minutes the OpenTodo way: from the due time, or from 09:00 for an all-day task (TickTick counts all-day triggers from midnight, so they differ by 540). They sync like any other field (hashes, TickTick wins conflicts).
  - TickTick `reminders[]` `TRIGGER:` durations ↔ OpenTodo `relative` reminders (`/api/v1/reminders`, separate records the client reconciles). TickTick's own spelling is kept while it means the same (`-PT0S`); non-trigger entries are never dropped.
  - OpenTodo `absolute`, `deadline` and location reminders have no TickTick equivalent: left alone, never synced or deleted.
  - No reminder hash migration was needed: tasks without reminders keep their old hashes. The first dry run (2026-11-22) planned 162 reminder pushes to OpenTodo and 2 OpenTodo-only reminders to TickTick, nothing else.
- **Closed TickTick lists / archived OpenTodo projects** are out of scope. Their tasks are left alone.
- **Recurrence (phase 2, 2026-11-22).** Rules sync both ways (`recurrence.py`, a port of OpenTodo's own RRULE converter). Report first with `--recurrence-report` (read-only).
  - A TickTick rule OpenTodo can't express exactly is kept in TickTick only (`ticktick-only:` in state) and never cleared from the OpenTodo side; that task syncs its due date only. If TickTick already holds the same rule in a different spelling, its spelling is kept.
  - TickTick `repeatFrom` `0` and `2` both mean "from the due date" (confirmed by Jesse for the daily tasks); `1` is OpenTodo's `after completion`.
  - Completing a repeating task in OpenTodo (task moves on, plus a completed occurrence record with `series_id`) completes it once in TickTick and doesn't write OpenTodo that pass. TickTick then advances it, and its next date wins on the following pass. Skipping in OpenTodo just moves the TickTick date.
  - Completing in TickTick only moves the OpenTodo date: OpenTodo gets no occurrence record for it (its completion history lacks those).
  - Jesse's 5 untranslatable rules were reworked in TickTick on 2026-11-22: water filter and gas split into two tasks each, alarm moved to Dec 1, AMEX YEAR no longer repeats, AMEX QUARTER due on the quarter's 1st and left overdue until done (don't push it back).

## Logging and ops

- Log to stdout (`docker compose logs ticktick-opentodo-sync`). One summary line per pass: created / updated / completed / deleted / conflicts per side.
- One-shot runs exit non-zero on errors. In loop mode, the Docker `HEALTHCHECK` (`ticktick-opentodo-sync --health`) reports failures, so `docker ps` shows `unhealthy`.
- `--status` prints the last run time, counts and any blocked bulk-delete (`docker compose exec ticktick-opentodo-sync ticktick-opentodo-sync --status`).
- One-off runs and the loop container share the SQLite file. A file lock in `/data` serializes them.

## About me

I'm a DevOps engineer. Keep the code idiomatic and typed (type hints, `ruff` + `mypy`-clean), and skip beginner explanations. Ask me before adding dependencies.
