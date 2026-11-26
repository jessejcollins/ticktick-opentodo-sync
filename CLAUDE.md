# TickTick ↔ OpenTodo Sync

A small Python service that keeps tasks synced between **TickTick** (primary) and a self-hosted **OpenTodo** instance (secondary). It runs as a Docker Compose service, typically on a small always-on box such as a Raspberry Pi that also runs Pi-hole.

These are the project's working rules for contributors and for Claude Code sessions. Personal, per-checkout notes belong in `CLAUDE.local.md` (gitignored), which Claude Code also loads.

## Goal

The same tasks show up in both apps, so someone can keep using TickTick day to day while trying out OpenTodo, and switch over cleanly later. Sync goes both ways, but **TickTick is the source of truth and wins every conflict.**

## Environment

- **Runtime:** Docker + Docker Compose. The image is built on the host from a git checkout (`docker compose up -d --build`), with no registry. Base image: `python:3.13-slim` (multi-arch: the same Dockerfile builds on arm64 and amd64).
- **Dev:** lint, type-check and test inside Docker: `docker build --target test .` (no local Python needed).
- **TickTick:** cloud, accessed through the TickTick Open API with a **personal API token** (TickTick → Settings → Account → API Token), not OAuth2. Developer docs: <https://developer.ticktick.com> (the readable source is <https://developer.ticktick.com/docs/openapi.md>).
- **OpenTodo:** self-hosted, reached over HTTPS through its REST API with an API token. Docs: <https://open-todo.app/docs>. End-to-end encryption may be on.
- **Scheduling:** an in-container loop (`ticktick-opentodo-sync --loop`) under `restart: unless-stopped`. Docker Compose is the supported deployment; the README also documents running from a virtualenv under cron or systemd (examples only: the repo ships no units).

## Hard constraints

- **Be a good neighbour on a shared host.** The compose service runs as a non-root UID, with `mem_limit: 128m`, `cpus: 0.25`, `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, a tmpfs for `/tmp` and `/run/ticktick-opentodo-sync`, and the `/data` state volume as its only writable path. No published ports, no host networking.
- **Few dependencies.** The standard library plus `httpx` (HTTP) and `cryptography` (OpenTodo E2EE). Stdlib `sqlite3` for state. No ORMs or frameworks. Ask the maintainer before adding a dependency.
  - The image installs them, and the setuptools that builds the package, from `requirements.lock`: hash-pinned, wheels only.
  - Regenerate the lock with the README's "Updating dependencies" command whenever `dependencies` changes.
  - The GitHub Action in the release workflow is pinned to a commit.
- **Secrets stay out of git.** Config comes from `.env` (gitignored) next to `compose.yaml`, loaded with `env_file:`. `.env.example` documents every setting. Never print secret values in logs or output.
- **Go easy on the SD card.** Write state only when something changed, in a single SQLite file. The per-pass heartbeat goes to tmpfs. Docker logs use `json-file` with small rotation.
- **Never delete in bulk by accident.** If a pass would delete or complete more than N tasks (configurable, default 10), it stops, logs a warning and records the block. In one-shot mode it exits non-zero. In loop mode the container keeps running and reports unhealthy until a run with `--allow-bulk` clears the block.
  - The same applies across passes: deletions over any 24 hours are capped too (`MAX_DELETES_PER_DAY`, default 50; `--allow-bulk` deletions don't count), so a fault that deletes a few tasks every pass is caught as well.
  - The habit guard (check-ins cleared, in either app) blocks the same way.
  - So do more than `MAX_CREATES` new tasks in one pass (default 50; not the first seed, not checklist items). That is most likely lost pairs, e.g. an older state backup restored, about to be duplicated in both apps.
- **Code style:** idiomatic, fully typed Python; `ruff` and `mypy --strict` clean.

## Sync rules

TickTick is primary. For each task pair, keep a record of what each side looked like at the last sync (a content hash per side), so the sync can tell what changed on which side.

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

**Projects/lists:**

- TickTick lists map to OpenTodo projects by name. The mapping lives in SQLite.
- Missing projects are created on the other side, but a live list never pairs with an archived project.
- A list deleted in TickTick archives its OpenTodo project, once the project's tasks have gone through the delete rules.
- A project deleted in OpenTodo is restored from its Trash (TickTick wins), or recreated if it's gone.
- List and project changes are applied only after the pass's guards.
- The TickTick Inbox maps to the OpenTodo Inbox (`project_id: null`).

### Fields

Synced: title, notes/description, due date (+ time and time zone), priority, tags ↔ labels, completed status, list/project, recurrence rules, subtasks and checklist items, reminders, and habits with their check-ins.

Not synced (could be added): comments, focus/pomodoro data.

Not possible with TickTick's Open API: attachments, Filters ↔ Views. See the decisions log.

## API facts

Checked against the live docs and real responses. Details are in `docs/api-notes.md`; update that file when something turns out different in a real response, and mark anything not confirmed as **UNVERIFIED**.

## Layout

```text
ticktick-opentodo-sync/
  CLAUDE.md                # this file (CLAUDE.local.md: personal, gitignored)
  README.md                # what it does, setup, operations
  LICENSE                  # MIT
  .github/workflows/release.yml  # tag + GitHub release when a version bump lands on main
  Dockerfile               # stages: build, dev, test (ruff+mypy+pytest), runtime
  requirements.lock        # runtime deps + build backend, hash-pinned (README: Updating dependencies)
  compose.yaml
  pyproject.toml
  .env.example
  src/ticktick_opentodo_sync/
    __main__.py            # CLI: `ticktick-opentodo-sync [--dry-run|--apply] [--loop [N]] [--status] ...`
    config.py
    http.py                # shared httpx client: throttle + retry (only what's safe to repeat)
    ticktick.py            # TickTick API client
    opentodo.py            # OpenTodo API client (E2EE, reminders reconciled here)
    e2ee.py                # OpenTodo end-to-end encryption
    recurrence.py          # RRULE <-> OpenTodo rule wording (port of OpenTodo's converter)
    models.py              # one normalized Task shape used by both sides
    mapping.py             # field mapping (priority, dates, tags, reminders) + project reconciliation
    state.py               # SQLite: id pairs, per-side hashes, project map, habit state, meta
    engine.py              # pure diff + sync rules above
    habits.py              # pure habit mapping + planning (definitions, daily check-ins)
    habit_sync.py          # habit orchestration, run after the task pass
    sync.py                # orchestration: fetch, plan, guard, apply, record
  tests/                   # pytest; in-memory fakes and respx; engine and habit planning tests are pure
  docs/api-notes.md
```

`engine.py` is pure logic: it takes the previous state plus both current snapshots and returns a list of actions. That way every row of the sync rules table can be unit-tested without network calls. `habits.py` follows the same pattern.

## Rollout (for a new installation)

1. **`--dry-run` first.** Log the actions it *would* take without writing anything. This is the default (`TICKTICK_OPENTODO_SYNC_DRY_RUN=true`) until turned off.
2. **Seed OpenTodo from TickTick** with OpenTodo empty: let the sync create everything. If OpenTodo already holds tasks, the one-time `--match-existing` mode pairs them by project + exact title and reports anything it can't pair.
3. Run it by hand a few times (`docker compose run --rm ticktick-opentodo-sync --apply`) and check both apps.
4. Set `TICKTICK_OPENTODO_SYNC_DRY_RUN=false` and `docker compose up -d`.
5. With OpenTodo encryption on, consider purging OpenTodo's plaintext history once the sync has run cleanly for a while (`POST /api/v1/encryption/purge-history`): it can hold unencrypted copies from before encryption was turned on. Until then, that history is a recovery source.

## To review later

- **Subtasks of repeating tasks in OpenTodo aren't unticked when the task repeats.** OpenTodo's app (`web/src/lib/ops.ts` `completeTask`, checked at commit `229c34b`) records the occurrence and moves the task on without touching its subtasks, and has no setting for it. While TickTick is primary this doesn't matter: TickTick unticks its checklist and the sync copies that over. For an OpenTodo-only future, either ask OpenTodo for the feature or make the sync untick a task's subtasks when it sees a completed OpenTodo occurrence.

## Decisions log

Settled answers from the build. Dates are when they were decided or learned.

- **TickTick auth:** personal API token in `TICKTICK_TOKEN`. No OAuth flow, no refresh logic. (TickTick's OAuth docs only describe the `authorization_code` grant, with no refresh grant and no documented lifetime.)
- **Dependencies:** runtime `httpx`, plus `cryptography` (for OpenTodo E2EE: the stdlib has no AES). No `python-dotenv`: compose's `env_file:` loads `.env`, and `config.py` has a small stdlib parser for runs outside Docker. Dev extra: `pytest`, `respx`, `ruff` (pinned: `ruff format --check` changes with its releases), `mypy`.
- **Docker:** deployed with Docker Compose, built on the host, scheduled by an in-container loop (not systemd, not cron).
- **Naming:** everything is `ticktick-opentodo-sync`: CLI command, Docker image, Compose service, container, volume (`ticktick-opentodo-sync-data`), Unix user, paths and files. Where hyphens aren't allowed, the import package is `ticktick_opentodo_sync` and the env var prefix is `TICKTICK_OPENTODO_SYNC_`.
- **OpenTodo end-to-end encryption** (2026-11-21, learned after it broke a sync). With encryption on, every REST read returns task `title`/`description` and project/label `name` as `enc:v1:<base64url(nonce|AES-256-GCM|tag)>`; the server never decrypts. An early version read that ciphertext as "changed in OpenTodo" and pushed it into TickTick; the tasks were restored from `GET /api/v1/tasks/{id}/history`.
  - **Decision: the sync decrypts and encrypts like OpenTodo's own apps** (`e2ee.py`, used inside `opentodo.py` so everything above it sees plaintext). Secret in `.env`: `OPENTODO_ENCRYPTION_RECOVERY_KEY` or `OPENTODO_ENCRYPTION_PASSPHRASE`. Accepted trade-off: someone with access to the host running the sync can read OpenTodo; the OpenTodo server still can't. Nothing plaintext is written to OpenTodo: creates send a placeholder, then PATCH the ciphertext bound to the new id (REST creates can't take a client id).
  - **Placeholder race (2026-11-22):** a browser open on OpenTodo can save its encrypted copy of the create placeholder *after* the sync's PATCH (seen 0.4 s later), wiping the content. The placeholder is never an OpenTodo edit: for a paired task or subtask, the engine uses the last-synced title/notes in its place, applies the normal rules, and rewrites OpenTodo (TickTick wins). An unpaired placeholder task (interrupted create) is left alone with a warning, and the TickTick client refuses any write containing the placeholder.
  - Safety: without a usable key the pass refuses to run; values that don't decrypt keep their task/project/label out of scope; the TickTick client refuses any write containing `enc:vN:`.
  - Fail closed (2026-11-25): a set secret means encryption is expected. Unless `GET /api/v1/me` reports `encryption.enabled: true`, the pass stops (`EncryptionExpectedError`), and the client never writes plaintext while a secret is set. Before, a server answering "off", or a `/me` that changed shape, got writes in plaintext, decrypted with the key cached from earlier passes. Without a secret, a `/me` with no `encryption` field is a server without E2EE (plaintext). A malformed `encryption` field stops the pass either way.
- **TickTick "Won't Do"** (status `-1`): treated as completed. OpenTodo has no equivalent state.
- **Seeding:** OpenTodo should start empty and be seeded from TickTick. `--match-existing` is the fallback. Safety net: a real run refuses to start with an empty pair table while OpenTodo has open tasks (e.g. leftovers from a failed import).
- **State write failures** (2026-11-25). A remote create whose pair couldn't be recorded (disk full) was created again by every later pass, and its copy was copied back, so the duplicates doubled each pass.
  - Before a pass first writes to either app, it proves the state is writable (`State.check_writable`, one small meta write). Passes with nothing to write still write nothing.
  - Any `sqlite3.Error` ends the pass; the per-action handlers re-raise it.
  - Remaining risk: a crash between a remote create and recording its pair still leaves one duplicate on each side.
- **A pair's TickTick list** (2026-11-25): `pairs.tt_project` is the list the task was last seen in, refreshed every pass (written only when it changed). TickTick's GET and DELETE take the list in the path. Whether they answer for a task under another list is unverified, so the fakes treat that as a 404 (see `docs/api-notes.md`).
- **Retries** (2026-11-25). Retrying everything after timeouts and 5xx repeated creates whose answer was lost (duplicates) and TickTick completions (a repeating task moved on twice).
  - **Repeated freely:** requests that are safe to repeat: GET, PUT, PATCH, DELETE, and a POST with an `Idempotency-Key` (OpenTodo dedupes it). OpenTodo project and label creates now carry one too.
  - **Marked repeatable:** TickTick's POST reads, task and habit updates, check-ins and moves (`idempotent=True`).
  - **Everything else** (TickTick creates and completions, OpenTodo habit logs and occurrence completions) is repeated only when the answer says it wasn't acted on: a connection error, 429, or 503 with `Retry-After`. Otherwise the action fails with a warning and the next pass reconciles. For task and habit creates that still means one duplicate when the lost request did go through; lists, projects and labels are matched by name next pass instead.
- **Tags ↔ labels:** OpenTodo has no tags. It has labels, referenced by ID. They are matched by lowercase name, and missing labels are created.
- **Priority mapping:** TickTick `0/1/3/5` (none/low/medium/high) ↔ OpenTodo `4/3/2/1` (1 is highest, 4 is the default).
- **Conflicts:** TickTick wins every field except completion. A completion made on either side is never undone by a conflict ("Completed in either app" takes priority).
- **Subtasks (2026-11-22).** One level deep on both sides; deeper levels are ignored.
  - TickTick **checklist items** ↔ OpenTodo **subtasks**, including checked items (as completed subtasks). Items have their own pair table (`item_pairs`; `tt_project` = TickTick parent id) and sync title and done state only. Order is copied on create (`sortOrder` → `position`), not afterwards. Items sync only while the parent is open and paired on both sides.
  - TickTick **child tasks** (`parentId`) ↔ OpenTodo subtasks as full tasks (`Task.parent`).
  - A **new OpenTodo subtask** becomes a checklist item under a TickTick CHECKLIST task, otherwise a TickTick child task (each app keeps its natural form). Subtasks of a brand-new parent follow one pass later.
  - Item conflicts: TickTick wins outright, done state included (a repeating TickTick checklist resets its items, which must not be re-ticked from OpenTodo). After a repeating task is completed in OpenTodo, its items are resynced from TickTick on the next pass.
  - The bulk guard counts item deletions in TickTick, not in OpenTodo (OpenTodo's copies can be recreated).
- **Recurrence (2026-11-22).** Rules sync both ways (`recurrence.py`, a port of OpenTodo's own RRULE converter). Report first with `--recurrence-report` (read-only).
  - A TickTick rule OpenTodo can't express exactly is kept in TickTick only (`ticktick-only:` in state) and never cleared from the OpenTodo side; that task syncs its due date only. If TickTick already holds the same rule in a different spelling, its spelling is kept.
  - TickTick `repeatFrom` `0` and `2` both mean "from the due date"; `1` is OpenTodo's `after completion`.
  - Completing a repeating task in OpenTodo (task moves on, plus a completed occurrence record with `series_id`) completes it once in TickTick and doesn't write OpenTodo that pass. TickTick then advances it, and its next date wins on the following pass. Skipping in OpenTodo just moves the TickTick date.
  - Completing in TickTick (2026-11-23): TickTick moves the task on under the same id and files a completed copy with the old due date. When a repeating task's TickTick date moves past the last-synced one, the sync looks for that copy (`POST /open/v1/task/completed`, same list + title + old date) and, if found, completes OpenTodo's occurrence for that date (`POST /api/v1/tasks/{id}/occurrences/{date}/complete`, with TickTick's completion time), then sets TickTick's next date. No copy = a reschedule: OpenTodo just moves. "After completion" rules and refusals (400) just move too. Past completions can't be backfilled: OpenTodo refuses dates before the current occurrence.
- **Reminders (2026-11-22).** `Task.reminders` holds offsets in minutes the OpenTodo way: from the due time, or from 09:00 for an all-day task (TickTick counts all-day triggers from midnight, so they differ by 540). They sync like any other field (hashes, TickTick wins conflicts).
  - TickTick `reminders[]` `TRIGGER:` durations ↔ OpenTodo `relative` reminders (`/api/v1/reminders`, separate records the client reconciles). TickTick's own spelling is kept while it means the same (`-PT0S`); non-trigger entries are never dropped.
  - OpenTodo `absolute`, `deadline` and location reminders have no TickTick equivalent: left alone, never synced or deleted.
  - Tasks without reminders keep their pre-reminder hashes, so adding the field needed no migration.
- **Habits (2026-11-24).** TickTick habits ↔ OpenTodo habits (repeating tasks with `habit: true`), both ways. `habits.py` is pure like `engine.py`; `habit_sync.py` runs after the task pass on the tasks it fetched.
  - Definitions sync like tasks (`habit_pairs`, per-side hashes, TickTick wins): name ↔ title, `repeatRule` ↔ `recurrence` (via `recurrence.py`), section `_morning`/`_afternoon`/`_night` ↔ day part morning/afternoon/evening, reminder times `HH:MM` ↔ relative reminders counted from 09:00.
  - Check-ins sync both ways over a rolling 30-day window, which is also the first-run backfill. One boolean per day: TickTick status 2 ↔ an OpenTodo done or tiny record. `habit_days` remembers the days done on both sides, so exactly one side can have changed. OpenTodo's "skipped" has no TickTick equivalent and stays there.
  - OpenTodo takes a check-in only from a habit's first day (its due date or earliest record) to today, and only completes the current day. So a new OpenTodo habit starts on its first check-in day in the window, and before logging a later day the sync moves the habit's due date to that day (the days in between read as missed, as they were).
  - A day OpenTodo refuses (400), such as a TickTick back-fill before the habit's first day there, stays checked in TickTick only (2026-11-25). It goes to `habit_refused`, never to `habit_days`: recording it as synced made the next pass read OpenTodo's gap as an un-check and clear the day in TickTick. Refused days aren't offered again while they stay checked in TickTick.
  - Logging an overdue day completes OpenTodo's current occurrence, which jumps the habit past today (seen 2026-11-24). So after the day writes, a daily habit whose due is after today, with today neither done nor skipped, is moved back to today.
  - The create-placeholder race (see E2EE above) can hit habits too: a paired OpenTodo habit whose title is the placeholder is rewritten from TickTick, never copied. It hit the "Habits" project's name too (2026-11-24): a paired project (or the habits project) named with the placeholder is renamed back at the start of each pass; an unpaired one is left alone.
  - New TickTick habits go to an OpenTodo project "Habits" (id in meta `ot_habits_project`), which the task sync never turns into a TickTick list. New OpenTodo habits are created in TickTick as yes/no habits.
  - Pairing by name (2026-11-25). After a state reset every habit is unpaired; copying them doubled each one on both sides, and TickTick habits can't be deleted through the API. The old "Habits" project, unknown without its meta, became a TickTick list.
    - Unpaired habits with the same name on both sides are paired instead (`match_habits`, TickTick wins). A name on both sides that isn't unique on one is left alone with a warning.
    - Without `ot_habits_project`, a live project named "Habits" that holds open habits is taken as the habits project again. Collect therefore fetches OpenTodo's tasks before reconciling projects.
  - Not synced: archiving/graduating and deleting (TickTick's API can't delete a habit), amount habits and targets ("3 times a week"); they're left alone with a warning. Clearing more check-ins than the bulk limit, in either app, blocks the habit pass. OpenTodo's side counts too: one empty check-in answer from TickTick read as every synced day un-checked (2026-11-25).
- **Not possible with TickTick's Open API (checked 2026-11-24):**
  - **Filters ↔ Views:** the Open API has no saved-filter endpoints (only an ad-hoc `/task/filter` query). OpenTodo Views (`/api/v1/views`) would be easy; TickTick is the blocker.
  - **Attachments:** TickTick keeps them as `![file](<id>/<name>)` references in the notes, which already sync as text. The Open API has no attachment routes, and the web app's `/api/v1/attachment/...` answers 401 to the API token. It needs a username/password session, which goes against the token-only decision.
- **Closed TickTick lists / archived OpenTodo projects** are out of scope. Their tasks are left alone.
- **Deleted lists and projects** (2026-11-25). Two problems before: a deleted list or project came back (its pair went stale, and the twin on the other side was created again), and `collect()` created lists and projects before the guards ran, so a BLOCKED or REFUSED pass had already written.
  - **Plan, then apply:** `collect()` only plans list/project changes (`World.projects`). `run()` applies them after the guards, and collects again when lists or projects were created, renamed or restored (at most once).
  - **List deleted in TickTick:** the OpenTodo project is "retiring". Only its paired tasks are in scope, so they follow the delete rules (bulk-guarded), and new OpenTodo tasks in it are never copied to TickTick. It's archived at the end of the pass, and only after a pass without errors. A failed archive counts as an error and is tried again next pass; the rest of the pass (habits, bookkeeping) still runs.
  - **Project deleted in OpenTodo:** a delete is a tombstone that only hides the project's tasks (`project_id` kept). Restoring it (`deleted_at: null`) brings them back. A project gone from the listing altogether is recreated, as before; purging also purges its tasks, which are then deleted in TickTick through the bulk guard.
  - **Name matching:** a live list never pairs with an archived project, otherwise its tasks would never sync.
  - **Missing from one listing** (2026-11-26). A list or project absent from a single listing was taken as deleted. A list's project was archived, and the list's tasks, still paired with copies in it, stopped syncing for good. A project was recreated, and once listed again became a second TickTick list that took the original list's tasks.
    - **List:** archived only once it's really gone. A paired task looked up and found still in that list (`World.tt_lists_alive`) keeps the project, with a warning.
    - **Project:** a paired project missing from the listing is looked up (`GET /api/v1/projects/{id}`). Only a 404 means gone; one that answers is used, trashed or not.

## Git workflow

Every change goes through a pull request.

- **Never commit to `main` or fast-forward it.** Start each feature, fix or doc change on a new branch from an up-to-date `main`: `feat/<name>`, `fix/<name>` or `docs/<name>`.
- **Open a PR with `gh pr create`** once the branch is pushed. The description covers what changed, why, and how it was tested (`docker build --target test .`, plus any dry run against real data). Keep pushing to the same branch for follow-ups.
- **Don't merge a PR unless the maintainer asks.**
- **Releases are automatic.** Bump `version` in `pyproject.toml` and `__version__` in `src/ticktick_opentodo_sync/__init__.py` (they must match) in their own PR. When it merges, `.github/workflows/release.yml` tags `vX.Y.Z` on `main` and publishes a GitHub release with notes generated from the merged PRs. Merges without a bump release nothing.
- **Stacked PRs:** before merging a PR that another PR is based on, retarget the dependent PR to `main` (`gh pr edit N --base main`). Deleting a base branch closes the PRs built on it instead of retargeting them.

## Logging and ops

- Log to stdout (`docker compose logs ticktick-opentodo-sync`). One summary line per pass: created / updated / completed / deleted per side, for tasks, checklist items and habits, plus conflicts and errors.
  - It also shows how many OpenTodo tasks were read and the process's peak memory against the container's limit, with a warning past 80%. The OpenTodo list (completed, trashed, habit days) only grows, and every pass holds all of it in 128 MB; trimming what's read risks "absent = deleted", so it's measured first.
- After a task update, what the API sent back is compared with what was sent. A field that didn't take is logged as a warning (`… didn't take the due of …`), because the readback's hash is what's recorded and the difference would otherwise go unnoticed. Intended differences are excluded: a repeating task's rule cleared on completion, TickTick-only rules, a completion that follows the update.
- One-shot runs exit non-zero on errors. In loop mode, the Docker `HEALTHCHECK` (`ticktick-opentodo-sync --health`) reports failures, so `docker ps` shows `unhealthy`.
- `--status` prints the last run time, counts and any blocked bulk-delete (`docker compose exec ticktick-opentodo-sync ticktick-opentodo-sync --status`).
- One-off runs and the loop container share the SQLite file. A file lock in `/data` serializes them.
