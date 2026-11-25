# ticktick-opentodo-sync

Two-way sync between **[TickTick](https://ticktick.com)** and a self-hosted **[OpenTodo](https://open-todo.app)**. Keep using TickTick day to day while you try OpenTodo, with the same tasks, lists and habits in both, then switch over when you're ready.

TickTick is the primary side: when both apps change the same thing between passes, TickTick's version wins. The sync runs as a small, locked-down Docker Compose service, makes one pass every 5 minutes, and is gentle enough to share a Raspberry Pi with Pi-hole.

> This is an unofficial tool. It isn't affiliated with or endorsed by TickTick or OpenTodo. It uses each app's public API with tokens you create.

## Use at your own risk

This tool writes to both of your task apps: it creates, changes, completes and deletes tasks, and a mistake is copied to the other side within minutes. It works well for its author, but your data, settings and edge cases will differ, and it comes with **no warranty** (see the [MIT license](LICENSE)). Before you trust it with tasks you care about:

- **Back up both apps first.** Use TickTick's backup in the web app (Settings → Account → Backup) and OpenTodo's JSON export (`GET /api/v1/export`). Keep backups from before the first `--apply`.
- **Start with a dry run** (the default) and read what it would do. Then do the first real runs by hand, and only then start the loop.
- **Keep the bulk-delete guard on.** Don't raise `TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE` much above the default, and only use `--allow-bulk` after reading what it would apply.
- **Remember TickTick wins conflicts.** An edit made in OpenTodo can be overwritten if the same task changed in TickTick before the next pass.
- **If something looks wrong,** stop it at once (`docker compose stop`, or stop your cron job or service), then compare both apps. OpenTodo keeps each task's history (`GET /api/v1/tasks/{id}/history`) and a Trash, which helps with recovery.

Bug reports with steps to reproduce are very welcome. Leave real task content out of them.

## What syncs

Everything below syncs both ways.

| | TickTick | OpenTodo | Notes |
|---|---|---|---|
| Tasks | title, notes, due date/time/zone, priority | the same | Priority: none/low/medium/high ↔ 4/3/2/1 |
| Organisation | lists, tags | projects, labels | Lists and projects are matched by name; missing ones are created |
| Completion | done, Won't Do | completed | Won't Do counts as done. A completion on either side always sticks |
| Repeating tasks | repeat rules | recurrence | Completing one occurrence in either app is recorded as a completion in the other |
| Subtasks | checklist items and child tasks | subtasks | One level deep |
| Reminders | reminders | relative reminders | "At due time", "30 minutes before", "the day before at 9:00"… |
| Habits | habits and daily check-ins | habits and their days | Check-ins of the last 30 days; TickTick habits go to an OpenTodo project called **Habits** |
| Deletions | deleted tasks | tasks moved to the Trash | Protected by a bulk-delete guard |

OpenTodo's [end-to-end encryption](#opentodo-end-to-end-encryption) is supported.

## What can't be synced

Some things can't be synced because TickTick's Open API offers no way to read them. Others aren't supported yet.

| Not synced | Why |
|---|---|
| **Attachments** (files and images) | TickTick's API can't download or upload attachment files. A TickTick attachment shows up in the task's notes as a reference like `![file](…/report.pdf)`, and that text does sync, but the file itself stays in TickTick. Attach files in OpenTodo by hand if you need them there. |
| **Filters ↔ Views** | TickTick's API has no way to list or create saved Filters. (OpenTodo's Views could be written, but there is nothing to read on the TickTick side.) Recreate them by hand. |
| **Comments, focus/Pomodoro data** | Not supported yet. |
| **OpenTodo-only features** | Reminders at a fixed time, deadline and location reminders, "skipped" habit days: TickTick has no equivalent, so these stay in OpenTodo and are never removed by the sync. |
| **Habit extras** | Amount habits ("8 glasses"), target habits ("3 times a week"), archiving and deleting habits. These habits are left alone with a warning. |
| **Some repeat rules** | A TickTick rule OpenTodo can't express (e.g. "on the 6th and 10th of each month") stays in TickTick; that task syncs only its next date. `--recurrence-report` lists them. |
| **Deeper nesting, renames** | Sub-subtasks are ignored. Renaming a list or project isn't synced: lists are paired once by name, then by ID. |
| **Closed lists, archived projects** | Left alone. |

## Requirements

- A TickTick account (an API token is available under Settings → Account).
- An OpenTodo server you can reach over HTTPS, with an API token.
- Either a Linux host with Docker Engine and the Compose plugin (built and tested on a Raspberry Pi 4, arm64, and on amd64), or Python 3.11 or newer to [run it without Docker](#running-without-docker).

## Setup

```sh
git clone https://github.com/jessejcollins/ticktick-opentodo-sync.git
cd ticktick-opentodo-sync
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

Fill in:

| Variable | Where to get it |
|---|---|
| `TICKTICK_TOKEN` | TickTick web → avatar → **Settings → Account → API Token** |
| `OPENTODO_URL` | Your OpenTodo server, e.g. `https://todo.example.com` (without `/api/v1`) |
| `OPENTODO_TOKEN` | OpenTodo → **Settings → Integrations → API tokens**, scope **full** (`agent` tokens are limited to 30 writes a minute) |
| `OPENTODO_ENCRYPTION_RECOVERY_KEY` *or* `OPENTODO_ENCRYPTION_PASSPHRASE` | Only if OpenTodo end-to-end encryption is on. See [below](#opentodo-end-to-end-encryption) |

Leave `TICKTICK_OPENTODO_SYNC_DRY_RUN=true` for now. Every setting is described in [.env.example](.env.example).

Then build the image:

```sh
docker compose build
```

## First run

OpenTodo should start **empty**: the sync seeds it from TickTick. If OpenTodo already has open tasks and nothing has been paired yet, a real run refuses to start. Empty OpenTodo first, or [pair the existing tasks](#pairing-tasks-that-already-exist).

1. **Dry run.** This logs every action it would take and writes nothing:

   ```sh
   docker compose run --rm ticktick-opentodo-sync --dry-run
   ```

   Check that it found your TickTick Inbox (`TickTick inbox id is …`), then read the `DRY-RUN would …` lines and the summary at the end. If you use repeating tasks, `--recurrence-report` (read-only) shows how each rule translates.

2. **Seed for real**, once, by hand:

   ```sh
   docker compose run --rm ticktick-opentodo-sync --apply
   ```

   A large account takes a few minutes, because requests are spaced 0.3 s apart.

3. **Check.** Make a few edits in both apps and run `--apply` again. A run with no edits in between should report all zeros.

4. **Start the loop.** Set `TICKTICK_OPENTODO_SYNC_DRY_RUN=false` in `.env`, then:

   ```sh
   docker compose up -d
   ```

## Running without Docker

If you'd rather not run Docker, the sync is an ordinary Python package with two dependencies (`httpx` and `cryptography`). It needs Python 3.11 or newer.

```sh
git clone https://github.com/jessejcollins/ticktick-opentodo-sync.git
cd ticktick-opentodo-sync
python3 -m venv .venv
.venv/bin/pip install .
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

Run commands from this directory: the sync reads `./.env` (or the file given with `--env-file`) and keeps its state in `./state/`. Set `TICKTICK_OPENTODO_SYNC_STATE_DIR` in `.env` to keep state somewhere else. The [first run](#first-run) steps are the same, minus the `docker compose run --rm` prefix:

```sh
.venv/bin/ticktick-opentodo-sync --dry-run
.venv/bin/ticktick-opentodo-sync --apply
.venv/bin/ticktick-opentodo-sync --status
```

To keep it running, pick one:

- **cron:** run one pass every 5 minutes. With `TICKTICK_OPENTODO_SYNC_DRY_RUN=false` in `.env`, no flag is needed. Overlapping runs are safe: a run that finds another holding the lock waits, then exits with code 3.

  ```cron
  */5 * * * * cd /path/to/ticktick-opentodo-sync && .venv/bin/ticktick-opentodo-sync >> sync.log 2>&1
  ```

- **systemd:** let the sync loop on its own (`--loop`, one pass every `TICKTICK_OPENTODO_SYNC_INTERVAL` seconds) as a service. For example, `/etc/systemd/system/ticktick-opentodo-sync.service`:

  ```ini
  [Unit]
  Description=TickTick <-> OpenTodo sync
  Wants=network-online.target
  After=network-online.target

  [Service]
  User=youruser
  WorkingDirectory=/path/to/ticktick-opentodo-sync
  ExecStart=/path/to/ticktick-opentodo-sync/.venv/bin/ticktick-opentodo-sync --loop
  # State goes to /var/lib/ticktick-opentodo-sync (the sync picks up STATE_DIRECTORY).
  StateDirectory=ticktick-opentodo-sync
  Restart=on-failure
  RestartSec=60

  [Install]
  WantedBy=multi-user.target
  ```

  Then `sudo systemctl enable --now ticktick-opentodo-sync` and follow it with `journalctl -u ticktick-opentodo-sync -f`.

Docker adds the resource limits and lockdown described in [Running next to Pi-hole](#running-next-to-pi-hole) and the `(healthy)`/`(unhealthy)` status. Without Docker, keep an eye on the log and exit codes instead. To update, `git pull` and run `.venv/bin/pip install .` again.

## Day to day

```sh
docker compose logs -f ticktick-opentodo-sync                                # follow the log
docker compose exec ticktick-opentodo-sync ticktick-opentodo-sync --status  # last pass, counts, blocked?
docker ps --filter name=ticktick-opentodo-sync                               # (healthy) / (unhealthy)
docker compose run --rm ticktick-opentodo-sync --dry-run                     # preview a pass
git pull && docker compose up -d --build                                     # update
```

Each pass logs the changes it made and one summary line:

```text
INFO TickTick: created=0 updated=1 completed=0 deleted=0 | OpenTodo: created=2 updated=0 completed=1 deleted=0 | items TickTick: created=0 updated=0 completed=0 deleted=0 | items OpenTodo: created=0 updated=1 completed=0 deleted=0 | habits TickTick: created=0 updated=0 completed=1 deleted=0 | habits OpenTodo: created=0 updated=0 completed=0 deleted=0 | conflicts=0 errors=0 (11.2s)
```

`items` are checklist items and subtasks. For `habits`, `completed` and `deleted` count check-ins added and cleared.

One-off runs (`docker compose run`) and the loop share the state volume. A lock file keeps them from overlapping, so one-offs are safe while the loop is running.

### Bulk-delete guard

If a pass would delete or complete more than `TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE` tasks (default 10), it writes nothing. It logs each blocked action, the container turns **unhealthy**, and `--status` shows `BLOCKED`. Every later pass blocks again until you act. If the changes are intended, apply them once with:

```sh
docker compose run --rm ticktick-opentodo-sync --apply --allow-bulk
```

The same limit applies to habit check-ins being cleared in TickTick.

### Exit codes

`0` ok · `1` errors (an API failure, or some actions failed) · `2` bad configuration · `3` blocked (bulk-delete guard, first-sync guard, or another run holds the lock).

## How it behaves

- **Change detection:** neither API can list only what changed, so each pass reads all open TickTick tasks (one request per list) and all OpenTodo tasks. Each pair of tasks stores a fingerprint of both sides from the last pass. After every write the new fingerprint is saved at once, so the sync never mistakes its own writes for your edits.
- **State file:** before a pass writes to either app, it checks that it can write its state file. If it can't (a full disk, a failing SD card), the pass stops there, writes nothing to either app, and the container shows unhealthy until the file can be written again.
- **Conflicts:** if both sides changed, TickTick's version overwrites OpenTodo, logged as `WARNING … [conflict]`. Completion is the exception: a task completed on either side stays completed.
- **Deletes:** deleting in TickTick moves the OpenTodo task to its Trash. Deleting in OpenTodo deletes in TickTick, unless the task was also edited in TickTick since the last pass; then TickTick wins and the task is recreated in OpenTodo.
- **Completed tasks:** TickTick's API only lists open tasks. When a paired task disappears, the sync fetches it once to tell *completed* from *deleted*.
- **Repeating tasks:** rules are translated between TickTick's iCalendar rules and OpenTodo's wording (`every 1st saturday of november`, `every 2 weeks after completion`) the same way OpenTodo's own converter does.
  - **Completing in OpenTodo** completes the task once in TickTick. TickTick then picks the next date, and OpenTodo follows on the next pass.
  - **Completing in TickTick** records that occurrence as completed in OpenTodo, and OpenTodo moves on to TickTick's next date. Rescheduling in TickTick just moves the date.
  - **Skipping in OpenTodo** moves the TickTick date.
  - **"After completion" rules** always just move the date.
- **Lists and projects:** matched by name, then by ID; a list or project that exists on one side only is created on the other.
  - Deleting a list in TickTick archives its OpenTodo project, after its tasks have gone to OpenTodo's Trash through the bulk-delete guard.
  - Deleting a project in OpenTodo brings it back from the Trash with its tasks (TickTick wins).
  - A new list is never paired with an archived project.
- **Subtasks:** TickTick checklist items appear in OpenTodo as subtasks, ticked items as completed ones, and TickTick child tasks as OpenTodo subtasks. Titles and ticks sync both ways, as do adding and deleting. A subtask added in OpenTodo becomes a checklist item if the TickTick task is a checklist, otherwise a TickTick child task. Order is copied when an item is created, not afterwards. On a conflict TickTick wins, including whether an item is ticked.
- **Reminders:** TickTick reminders ↔ OpenTodo relative reminders. For all-day tasks, TickTick counts from midnight and OpenTodo from 09:00, and the sync converts between the two.
- **Habits:** name, repeat, part of the day (morning/afternoon/evening ↔ TickTick's sections) and reminder times are kept in step. Ticking or un-ticking a day in either app is copied to the other, for the last 30 days. OpenTodo can't record a day before a habit's first day there, so ticking such a day in TickTick stays in TickTick only. New TickTick habits appear in an OpenTodo project called **Habits**; new OpenTodo habits are created in TickTick as yes/no habits. A habit with the same name in both apps is paired rather than copied, and TickTick's version wins.

## OpenTodo end-to-end encryption

With encryption on, OpenTodo's server only holds ciphertext for task titles and notes and for project and label names. The sync does what OpenTodo's own apps do. It fetches the wrapped key and unlocks it with your recovery key or passphrase. It then decrypts what it reads and encrypts what it writes, so no plaintext ever reaches the server.

- **Set one secret** in `.env`, in single quotes:
  - `OPENTODO_ENCRYPTION_RECOVERY_KEY` unlocks instantly.
  - `OPENTODO_ENCRYPTION_PASSPHRASE` costs a few seconds of CPU once per container start.

  Either one can read all your OpenTodo data, so keep `.env` at mode 600 and treat the host accordingly. The unlocked key lives only in memory.
- **No secret, or a wrong one:** every pass fails with a clear error (`cannot unlock`), the container shows unhealthy, and nothing is written.
- **Changing the passphrase or resetting the recovery key** elsewhere makes the stored one stop working. Update `.env` and restart.
- **Values that don't decrypt** keep their task, project or label out of the sync, with a warning. They're never mistaken for an edit or a deletion.
- **Safety checks:**
  - With a secret set, OpenTodo must report encryption on. If it says it's off, or stops saying, every pass stops instead of writing plaintext. If you turn encryption off in OpenTodo, remove the secret from `.env`.
  - The TickTick client refuses to write any encrypted value.
  - If a browser tab open on OpenTodo overwrites a just-created task, habit or project with the sync's temporary placeholder, the next pass puts the content back from TickTick.

## Pairing tasks that already exist

If OpenTodo already holds copies of your TickTick tasks, pair them instead of creating duplicates:

```sh
docker compose run --rm ticktick-opentodo-sync --match-existing --dry-run   # report only
docker compose run --rm ticktick-opentodo-sync --match-existing --apply     # record the pairs
```

Tasks are paired by list/project name plus exact title, when that combination is unique on both sides. Unmatched OpenTodo tasks are listed: the next sync would copy them into TickTick, so delete or fix them first. After pairing, TickTick's content wins for every pair on the next sync.

## Running next to Pi-hole

The service is built to share a small host:
- **Resource limits:** it runs as UID 10001 with 128 MB of RAM (no swap) and 0.25 CPU at a low CPU weight, and a 64-process limit.
- **Lockdown:** the root filesystem is read-only, with `cap_drop: ALL`, `no-new-privileges` and no published ports.
- **Writes:** it only writes to its data volume (SQLite, and only when something changed) and two small tmpfs mounts.
- **Logs:** Docker logs rotate at 3 × 1 MB.

**DNS:** Docker can't forward to a `127.0.0.1` resolver. If the host's own `resolv.conf` points at Pi-hole on loopback, containers fall back to public DNS. To resolve through Pi-hole, uncomment `dns:` in [compose.yaml](compose.yaml) and set it to the host's LAN address.

## State, backup and reset

State lives in the Docker volume `ticktick-opentodo-sync-data`, in `/data/ticktick-opentodo-sync.sqlite3`. It holds:
- the task, item and habit pairs, with their per-side fingerprints;
- the list ↔ project map;
- a few settings;
- the last-synced copy of each task (title, notes, dates…), used to tell which side changed.

That last item is task content, decrypted, so treat the volume as private.

```sh
docker compose cp ticktick-opentodo-sync:/data/ticktick-opentodo-sync.sqlite3 ./backup.sqlite3   # backup
docker compose down && docker volume rm ticktick-opentodo-sync-data                              # full reset
```

After a reset, every task looks new on both sides. Empty OpenTodo or use `--match-existing` before syncing again. Habits are paired again by name on the next pass, and the **Habits** project is recognised again: a project with that name holding habits.

## Development

You only need Docker. The `test` stage runs `ruff check`, `ruff format --check`, `mypy --strict` and `pytest`:

```sh
docker build --target test .
```

For a faster loop, build the `dev` stage once and mount the source:

```sh
docker build --target dev -t ticktick-opentodo-sync:dev .
docker run --rm -v "$PWD":/w -w /w -e PYTHONPATH=/w/src ticktick-opentodo-sync:dev pytest -q
```

Outside Docker (Python 3.11 or newer): `pip install -e '.[dev]'` in a virtualenv, then `pytest`, `ruff check src tests` and `mypy`. See [Running without Docker](#running-without-docker) for running the sync itself.

### Code map

- **Pure sync rules:** `engine.py`. Every row of the rules table has a test in `tests/test_engine.py`. `habits.py` is its counterpart for habits.
- **Orchestration:** `sync.py` and `habit_sync.py` fetch, plan, guard, apply and record.
- **Field mapping:** `mapping.py`, plus `recurrence.py` for repeat rules.
- **API clients:** `ticktick.py`, `opentodo.py` (with `e2ee.py` for encryption) and the shared `http.py`.
- **State:** `state.py` (SQLite).
- **Test fakes:** `tests/fakes.py` has in-memory fakes of both APIs for end-to-end passes.

API details and the field-mapping table are in [docs/api-notes.md](docs/api-notes.md). Design decisions and project rules are in [CLAUDE.md](CLAUDE.md).

### Unconfirmed API behaviour

The sync handles these defensively, but TickTick's docs don't confirm them. They're marked **UNVERIFIED** in [docs/api-notes.md](docs/api-notes.md); reports either way are welcome. When either app ignores part of a write, the log says so, for example `WARNING TickTick didn't take the due of 'Report'`. Such a line is exactly the kind of report that helps.

1. Updating a TickTick task with `dueDate: null` clears its due date.
2. A deleted TickTick task answers 404 (or an empty answer, or carries a `deleted` flag).
3. A TickTick list with no open tasks may come back without its `tasks` field.

## Contributing

Issues and pull requests are welcome.
- Keep changes typed and passing `docker build --target test .`.
- Add a test for each sync rule you touch.
- Note anything you learn about the APIs in `docs/api-notes.md`.
- New runtime dependencies need a good reason: the project deliberately sticks to `httpx` and `cryptography`.

Please don't include real task data, tokens or keys in issues, logs or tests.

## License

[MIT](LICENSE)
