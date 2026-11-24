# ticktick-opentodo-sync

Two-way task sync between **TickTick** (primary, wins every conflict) and a self-hosted **OpenTodo**. It runs as a small, locked-down Docker Compose service on a Raspberry Pi next to Pi-hole and makes one pass every 5 minutes.

- Syncs title, notes, due date/time/zone, priority, tags ↔ labels, completion, list ↔ project, repeat rules, reminders, subtasks (TickTick checklist items and subtasks ↔ OpenTodo subtasks), and habits with their check-ins.
- Not synced: attachments and Filters (TickTick's API has no way to read them), comments, focus data.
- Rules, decisions and constraints: [CLAUDE.md](CLAUDE.md). API details and the field-mapping table: [docs/api-notes.md](docs/api-notes.md).

## Setup on the Pi

Prerequisites: Docker Engine with the Compose plugin, and git.

```sh
git clone <this repo> ~/ticktick-opentodo-sync
cd ~/ticktick-opentodo-sync
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

Fill in:

| Variable | Where to get it |
|---|---|
| `TICKTICK_TOKEN` | TickTick web → avatar → **Settings → Account → API Token** |
| `OPENTODO_URL` | Your PikaPods URL, e.g. `https://todo.example.com` (no `/api/v1`) |
| `OPENTODO_TOKEN` | OpenTodo → **Settings → Integrations → API tokens**, scope **full** (`agent` tokens are rate-limited to 30 writes/min) |
| `OPENTODO_ENCRYPTION_RECOVERY_KEY` *or* `OPENTODO_ENCRYPTION_PASSPHRASE` | Only with OpenTodo end-to-end encryption on. See [OpenTodo end-to-end encryption](#opentodo-end-to-end-encryption) |

Leave `TICKTICK_OPENTODO_SYNC_DRY_RUN=true` for now. All settings are documented in [.env.example](.env.example).

Build the image on the Pi:

```sh
docker compose build
```

## First run (rollout)

OpenTodo should start **empty**: ticktick-opentodo-sync seeds it from TickTick. If OpenTodo already has open tasks and nothing has been paired yet (for example, leftovers from an aborted import), a real run refuses to start. You can then either empty OpenTodo's task list or pair the existing tasks (see [Pairing tasks that already exist](#pairing-tasks-that-already-exist)).

1. **Dry run.** It logs every action it would take and writes nothing to either app.

   ```sh
   docker compose run --rm ticktick-opentodo-sync --dry-run
   ```

   Check that it found your TickTick Inbox (`TickTick inbox id is inbox…`). Then read through the `DRY-RUN would create_ot …` lines and the summary at the end.

2. **Seed for real**, once, by hand:

   ```sh
   docker compose run --rm ticktick-opentodo-sync --apply
   ```

   A big TickTick account takes a while: requests are spaced 0.3 s apart. Lists are created in OpenTodo by name, and TickTick tags become OpenTodo labels.

3. Make a few edits in both apps and run `--apply` again a couple of times. Check both apps. A second run with no edits should report all zeros.

4. **Turn on the loop.** Set `TICKTICK_OPENTODO_SYNC_DRY_RUN=false` in `.env`, then:

   ```sh
   docker compose up -d
   ```

## Day-to-day

```sh
docker compose logs -f ticktick-opentodo-sync                 # one summary line per pass
docker compose exec ticktick-opentodo-sync ticktick-opentodo-sync --status  # pairs, last pass, last change, blocked?
docker ps --filter name=ticktick-opentodo-sync                # STATUS shows (healthy)/(unhealthy)
docker compose run --rm ticktick-opentodo-sync --dry-run      # ad-hoc preview
docker compose up -d --build                                  # after git pull
```

A summary line looks like this:

```text
INFO TickTick: created=0 updated=1 completed=0 deleted=0 | OpenTodo: created=2 updated=0 completed=1 deleted=0 | conflicts=0 errors=0 (3.4s)
```

One-off runs (`docker compose run`) and the loop container share the state volume. A lock file serializes them, so it's safe to run one-offs while the loop is up.

### Bulk-delete guard

If one pass would delete or complete more than `TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE` tasks (default 10), it writes nothing. It logs each blocked action, marks the container **unhealthy**, and `--status` shows `BLOCKED`. Every later pass blocks again until you act. If the deletions are intended:

```sh
docker compose run --rm ticktick-opentodo-sync --apply --allow-bulk
```

`--allow-bulk` applies to that one run only.

### Exit codes

`0` ok · `1` errors (API failure, or some actions failed) · `2` bad config · `3` blocked (bulk guard, first-sync guard, or another run holds the lock).

## How it behaves

- **Change detection:** neither API offers a modified-since filter, so every pass pulls all open TickTick tasks (one request per list) and all OpenTodo tasks. Each pair stores a content hash per side. After every write, the new hash is saved immediately, so the next pass doesn't see its own writes as edits.
- **Conflicts:** if both sides changed, TickTick's version overwrites OpenTodo (logged as `WARNING … [conflict]`). Completion is the exception: a task completed on either side stays completed.
- **Deletes:** deleting in TickTick deletes in OpenTodo (soft delete, into OpenTodo's Trash). Deleting in OpenTodo deletes in TickTick, unless the task was also edited in TickTick since the last pass; then it is recreated in OpenTodo.
- **Completed tasks:** TickTick's API only lists open tasks. When a paired task disappears, ticktick-opentodo-sync fetches it once to tell *completed* from *deleted*. TickTick's **Won't Do** counts as completed.
- **Repeat rules** sync both ways. TickTick's iCalendar rules are translated to OpenTodo's wording (`every 1st saturday of november`, `every 2 weeks after completion`) exactly as OpenTodo's own converter does. A rule OpenTodo can't express stays in TickTick, and that task syncs only its due date. Check before a first run: `docker compose run --rm ticktick-opentodo-sync --recurrence-report` (read-only).
- **Completing a repeating task:** in OpenTodo, it completes the task once in TickTick. TickTick then picks the next date, and OpenTodo follows on the next pass. In TickTick, OpenTodo records that occurrence as completed and moves to the next date; rescheduling in TickTick (no completion) just moves the date. Rules that repeat after completion always just move. Skipping in OpenTodo moves the TickTick date.
- **Reminders:** TickTick reminders ↔ OpenTodo *relative* reminders ("at due time", "30 minutes before", "1 day before at 09:00"…), both ways. OpenTodo reminders at a fixed moment, deadline reminders and location reminders stay in OpenTodo only.
- **Habits:** TickTick habits appear in an OpenTodo project called **Habits** (and OpenTodo habits in TickTick), with name, repeat, part of the day and reminder times kept in step. Check-ins of the last 30 days sync both ways; OpenTodo's *skipped* stays in OpenTodo. Amount and target habits ("8 glasses", "3 times a week") and archiving aren't synced.
- **Priority:** TickTick none/low/medium/high (`0/1/3/5`) ↔ OpenTodo `4/3/2/1`.
- **Subtasks:** TickTick checklist items appear in OpenTodo as subtasks (ticked items as completed ones), and TickTick subtasks as OpenTodo subtasks. Titles and ticks sync both ways; adding and deleting do too. A subtask added in OpenTodo becomes a checklist item if the TickTick task is a checklist, otherwise a TickTick subtask. One level only. Reordering isn't synced. On a conflict TickTick wins, including whether an item is ticked.
- **Out of scope** (left alone): closed TickTick lists, archived OpenTodo projects, sub-subtasks, OpenTodo habits and recurrence occurrences.
- **Project renames** aren't synced. Pairs are kept by ID after the first match by name.

## OpenTodo end-to-end encryption

With encryption on, OpenTodo's server only ever holds ciphertext for task titles and notes and for project and label names. ticktick-opentodo-sync does what OpenTodo's apps do. It fetches the wrapped key record (`GET /api/v1/encryption/keys`) and unwraps the account key with your recovery key or passphrase. It decrypts what it reads and encrypts what it writes, so no plaintext reaches the server.

- Set **one** secret in `.env`, in single quotes: `OPENTODO_ENCRYPTION_RECOVERY_KEY` (unlocks instantly) or `OPENTODO_ENCRYPTION_PASSPHRASE` (PBKDF2 with ≥ 600,000 iterations: a few seconds at 0.25 CPU, once per container start). Either one can read your OpenTodo data, so keep `.env` at mode 600. The key itself lives only in memory.
- Without a secret, or with a wrong one, every pass fails with a clear error (`docker ps` shows unhealthy). Nothing is written.
- Changing the passphrase elsewhere breaks a stored passphrase. Resetting the recovery key breaks a stored recovery key. The next pass reports `cannot unlock`; update `.env` and restart.
- A value that doesn't decrypt keeps its task (or project or label) out of the sync, with a warning. It is never mistaken for an edit or a deletion.
- As a last line of defence, the TickTick client refuses to write any `enc:v…` value.

## Pairing tasks that already exist

If OpenTodo already holds tasks that mirror TickTick, pair them instead of duplicating:

```sh
docker compose run --rm ticktick-opentodo-sync --match-existing --dry-run   # report only
docker compose run --rm ticktick-opentodo-sync --match-existing --apply     # record pairs
```

Tasks are paired by list/project name + exact title, when that key is unique on both sides. Unmatched OpenTodo tasks are listed, because the next sync would copy them into TickTick, so delete or fix them first. On the next sync, TickTick's content wins for every pair.

## Footprint and Pi-hole safety

The compose service runs as UID 10001 with a read-only root filesystem and `cap_drop: ALL`, `no-new-privileges`, 128 MB RAM (no swap), 0.25 CPU, a CPU weight of 256 and a 64-process limit. It has no published ports. Its only writable paths are the `ticktick-opentodo-sync-data` volume (SQLite, written only when something changed) and two small tmpfs mounts. Docker logs rotate at 3 × 1 MB.

**DNS:** Docker can't forward to a `127.0.0.1` resolver, so if the Pi's own `resolv.conf` points at Pi-hole on loopback, containers fall back to public DNS. To resolve through Pi-hole, uncomment `dns:` in [compose.yaml](compose.yaml) and set it to the Pi's LAN IP.

## State, backup, reset

State lives in the named volume `ticktick-opentodo-sync-data`, in `/data/ticktick-opentodo-sync.sqlite3`. It holds task ID pairs, per-side hashes, the list ↔ project map and a few meta keys.

```sh
docker compose cp ticktick-opentodo-sync:/data/ticktick-opentodo-sync.sqlite3 ./ticktick-opentodo-sync-backup.sqlite3   # backup
docker compose down && docker volume rm ticktick-opentodo-sync-data  # full reset
```

After a reset, every task looks new on both sides. Empty OpenTodo or use `--match-existing` before syncing again.

## Development

The dev machine needs only Docker. The `test` stage runs `ruff check`, `ruff format --check`, `mypy --strict` and `pytest`:

```sh
docker build --target test .
```

For a faster loop, build the `dev` stage once and mount the source:

```sh
docker build --target dev -t ticktick-opentodo-sync:dev .
docker run --rm -v "$PWD":/w -w /w -e PYTHONPATH=/w/src ticktick-opentodo-sync:dev pytest -q
```

Outside Docker (Python ≥ 3.11): `pip install -e '.[dev]'`, then `ticktick-opentodo-sync --dry-run`. It reads `./.env` and keeps state in `./state/`.

Code map: `engine.py` holds the pure sync rules, and every row of the rules table has a test in `tests/test_engine.py`. `sync.py` is orchestration, `mapping.py` handles the field mapping, `state.py` is SQLite, and `ticktick.py`/`opentodo.py`/`http.py` are the API clients. `tests/fakes.py` has in-memory fakes of both APIs for end-to-end passes.

## Before trusting it: things the docs don't say

These are handled defensively, but confirm them on your first dry/real runs. They are tracked as **UNVERIFIED** in [docs/api-notes.md](docs/api-notes.md).

1. `GET /open/v1/project/inbox/data` returns the TickTick Inbox. If it 404s, you'll see a warning, and new Inbox tasks won't sync.
2. TickTick `POST /open/v1/task/{id}` with `dueDate: null` clears a due date.
3. A deleted TickTick task 404s on `GET` (or carries a `deleted` flag).
