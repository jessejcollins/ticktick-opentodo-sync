# API notes

Checked on 2026-11-21 against:

- TickTick: <https://developer.ticktick.com/docs/openapi.md> (the developer site is a JS app; this markdown file is its source)
- OpenTodo: <https://open-todo.app/docs/api/> and the generated spec
  `docs/openapi.json` in `github.com/OpenTodoApp/OpenTodo` (also served by every
  server at `GET /api/v1/openapi.json`, no auth). Docs version 1.6.0.

Items marked **UNVERIFIED** are not in the docs. The code handles them defensively. Confirm them on the first dry run and update this file.

## TickTick Open API

### Auth

- **Personal API token** (TickTick web → avatar → Settings → Account → API Token). It is sent as `Authorization: Bearer <token>`. This is what ticktick-opentodo-sync uses.
- OAuth2 exists: `https://ticktick.com/oauth/authorize` and then `POST https://ticktick.com/oauth/token` (Basic auth with client id/secret, form-encoded, `grant_type=authorization_code`, scopes `tasks:read tasks:write`). The docs describe **no refresh grant** and **no token lifetime**. Not used.
- Revoke: `POST /oauth/revoke` with `token=`.

### Base URL and endpoints

Base: `https://api.ticktick.com`

| Purpose | Method + path | Notes |
|---|---|---|
| User preferences | `POST /open/v1/preference` | `{"timeZone": "America/Los_Angeles"}`; used as the default zone |
| List projects | `GET /open/v1/project` | optional `offset`/`limit` (default 200). Project: `id,name,color,sortOrder,closed,groupId,viewMode,permission,kind` (`TASK`/`NOTE`) |
| Project + tasks | `GET /open/v1/project/{projectId}/data` | `{project, tasks, columns}`. **`tasks` = undone tasks only** (ProjectData definition) |
| Get task | `GET /open/v1/project/{projectId}/task/{taskId}` | 200 Task / 404 |
| Create task | `POST /open/v1/task` | `title`, `projectId` required; returns Task |
| Update task | `POST /open/v1/task/{taskId}` | body must include `id`, `projectId`. **UNVERIFIED** whether omitted fields are cleared, so ticktick-opentodo-sync sends the full merged task |
| Complete task | `POST /open/v1/project/{projectId}/task/{taskId}/complete` | no body, no content |
| Delete task | `DELETE /open/v1/project/{projectId}/task/{taskId}` | |
| Move task | `POST /open/v1/task/move` | body: array of `{fromProjectId,toProjectId,taskId}`; returns `[{id, etag}]` |
| Batch | `POST /open/v1/task/batch` | `{add:[], update:[]}` ≤ 50 each; returns `id2etag`/`id2error` (`DELETED`, `NOT_EXISTED`, ...). Not used in v1 |
| Completed tasks | `POST /open/v1/task/completed` | `{projectIds,startDate,endDate}`, max 200 |
| Filter | `POST /open/v1/task/filter` | status/priority/tags filters, max 200 |
| Undone in range | `POST /open/v1/task/undone` | ≤ 14-day window; `projectIds` accepts `"inbox"` |
| Create project | `POST /open/v1/project` | `{name, color?, viewMode?, kind?}` returns Project |
| Tags | `GET /open/v1/tag` | `[{name,label,sortOrder,color,type}]` |

### Task object

`id, projectId, title, content, desc, isAllDay, startDate, dueDate, timeZone, reminders[], repeatFlag ("RRULE:..."), repeatFrom, priority, status, completedTime, sortOrder, items[] (checklist), tags[] (names), kind (TEXT|NOTE|CHECKLIST), parentId, assigneeUsername, focusSummaries`. Responses also carry `etag` (seen in the completed/filter examples).

- Dates are formatted `yyyy-MM-dd'T'HH:mm:ssZ`, e.g. `2019-11-13T03:00:00+0000`. Some examples include milliseconds (`2026-03-04T23:58:20.000+0000`), so the parser accepts both.
- `priority`: None `0`, Low `1`, Medium `3`, High `5`.
- `status`: Abandoned (Won't Do) `-1`, Normal `0`, Completed `2`. Checklist items use `0`/`1`.
- Checklist items (`items[]`: `id, title, status, sortOrder, completedTime, isAllDay, startDate, timeZone`) live inside the task. ticktick-opentodo-sync changes them by writing the whole task with the edited `items` list: new items without `id` get one from TickTick, and omitted items are removed (verified 2026-11-22). Child tasks carry `parentId`; parents list `childIds`. Creating a child with `parentId` works (**verified 2026-11-22**: the parent's `childIds` lists it). Ticking an item by writing the task with an edited `items` list works (verified 2026-11-22).
- `content` holds the notes for TEXT tasks. `desc` is the "description of checklist" for CHECKLIST tasks.
- **No `modifiedTime` on tasks**, and no modified-since filter. Every run is a full pull plus a hash diff.

### Inbox

- The docs never say whether the project list includes the Inbox. **UNVERIFIED.** Community reports say it does not.
- Several endpoints accept the literal `inbox` in place of a project ID (`completeTasks` defaults to the inbox; `task/undone` documents `"inbox"`). ticktick-opentodo-sync calls `GET /open/v1/project/inbox/data` (**UNVERIFIED**, check on the first dry run). It learns the real inbox ID (`inbox<digits>`) from the response's `project.id` or from its tasks' `projectId`.

### Deleted vs completed

- Project data returns only undone tasks. When a paired, previously open task disappears, ticktick-opentodo-sync calls `GET .../task/{id}`:
  - `404` → deleted.
  - Task with `deleted` truthy → deleted (**UNVERIFIED** field, handled in case trashed tasks are still returned).
  - `status` `2` or `-1` → completed.
  - `status` `0` → still open but out of scope (moved to a closed list or turned into a subtask). Left alone.
- If a task's last known state was completed, it is not refetched; it is assumed unchanged until it shows up open again.
- Caveat: a task moved to a list ticktick-opentodo-sync doesn't pull, then looked up under its old project ID, may 404 and be treated as deleted. The bulk-delete guard limits the blast radius.

### Rate limits

Not documented. ticktick-opentodo-sync spaces requests (`TICKTICK_OPENTODO_SYNC_HTTP_MIN_INTERVAL`, default 0.3 s) and retries 429/5xx with backoff, honouring `Retry-After`. One pass is about `3 + number of lists` requests plus one GET per task that disappeared.

### Recurrence

Completing a repeating task through the API (`.../complete`) moves it to its next date under the same id and **unticks its checklist items** (verified 2026-11-22). `repeatFlag` is an RRULE string plus `repeatFrom`, returned as a string (`"0"` from due date, `"1"` from completion date, `"2"` calendar; `"2"` behaves like `"0"` for the user's daily tasks). **Verified 2026-11-22:** an update with `repeatFlag: ""` removes the rule. Completing a recurring task keeps the same task ID and advances its dates. The completed instance gets a new ID (**UNVERIFIED**, observed behaviour of the apps). Updates merge over the full task, so fields the sync doesn't model are preserved.

### Reminders

`reminders` is a list of iCalendar `TRIGGER:` durations from the due time (`TRIGGER:PT0S` at due time, `TRIGGER:-PT30M`, `TRIGGER:-P1D`). For an all-day task they count from **midnight** of the day (`TRIGGER:P0DT9H0M0S` is 09:00 on the day, `TRIGGER:-P0DT15H0M0S` 09:00 the day before); the apps write that long form. `TRIGGER:-PT0S` occurs too and means the same as `PT0S`. Seen 2026-11-22: 162 of 465 open tasks have reminders, all `PT0S` (timed) or `P0DT7H0M0S` (all-day).

## OpenTodo REST API

### Auth and conventions

- Base path `/api/v1`. All requests and responses are JSON.
- `Authorization: Bearer ot_...` (workspace tokens: `otw_...`). Create one in Settings → Integrations → API tokens, or `POST /api/v1/tokens {name, scope}`. Scopes: `full` (needed for writes), `read`, `agent`, `admin`.
- Errors: `{"error": {"code": "invalid_input", "message": "..."}}`.
- **Writes with unknown fields are rejected whole** (400 `bad_request`), so payloads contain only known fields.
- `POST` accepts an `Idempotency-Key` header on every collection.
- Timestamps are Unix **milliseconds**. Dates are floating `YYYY-MM-DD`.
- Rate limits are documented only for auth, quick-add (60/min) and agent tokens (30 writes/min, 1,000 changed items/day). **Use a `full` token, not `agent`.** There is no general REST limit.

### Endpoints

| Purpose | Method + path | Notes |
|---|---|---|
| List tasks | `GET /api/v1/tasks` | `{tasks:[...]}`: open **and** completed. `?include_deleted=true` adds tombstones (`deleted_at` set). `?project_id=` filters. `completed_after/before` select only completed tasks. No pagination documented |
| Get task | `GET /api/v1/tasks/{id}` | |
| Create task | `POST /api/v1/tasks` | 201 returns task |
| Update task | `PATCH /api/v1/tasks/{id}` | partial; returns task |
| Complete / reopen | `PATCH` with `completed_at` | ms timestamp completes; `null` reopens. No dedicated route except for recurring occurrences |
| Delete task | `DELETE /api/v1/tasks/{id}` | soft delete to Trash (204); `DELETE .../purge` is permanent |
| Projects | `GET/POST /api/v1/projects`, `GET/PATCH/DELETE /api/v1/projects/{id}` | create: `name, color, parent_id, icon, flow_mode` |
| Labels | `GET/POST /api/v1/labels`, `.../{id}` | create `{name, color?}`; an existing label with the same name returns 200 |
| History | `GET /api/v1/tasks/{id}/history` | change sets from the op log |
| Webhooks | `/api/v1/webhooks` | `task.created/updated/completed/reopened/deleted`, HMAC-signed. Not used: the Pi isn't reachable from PikaPods |

### Task object (spec `components/schemas/Task`)

Relevant fields: `id` (string, UUID), `title`, `description` (Markdown), `project_id` (string or `null` = **Inbox**), `parent_id` (subtasks), `priority` (1–4, **1 highest, 4 default**), `due` (`{date: "YYYY-MM-DD", time?: "HH:MM", tz?: IANA}`; `tz` only with `time`; without `tz` the due is floating), `deadline`, `start`, `labels` (label **IDs**; writing replaces the whole list), `completed_at` (ms or null), `deleted_at` (ms or null), `created_at`, `recurrence` (rule string), `series_id`, `occurrence_date`, `habit`.

- **No `updated_at`** and no modified-since filter on tasks. A change feed exists (`/api/v1/sync`, `/api/v1/activity`, SSE `sync/events`, webhooks), but a hash diff is simpler and enough at this scale.
- No tags. Labels are separate objects.
- Recurring series: a task with `recurrence`, OpenTodo's canonical English phrase (`every 2 weeks on saturday`, grammar in OpenTodo `docs/recurrence.md`), **not** an RRULE. Writers store `every N weeks` when the due date is on the single weekday ("Compatible"). Occurrence records carry `series_id`/`occurrence_date`; a *completed* one (`completed_at` set, not `skipped`) means "this date of the series was done". Otherwise they are ignored.
- Completing a series (app, or REST `PATCH completed_at`) does not complete the task: it records a completed occurrence and moves the task to its next date. ticktick-opentodo-sync therefore sends `recurrence: null` with `completed_at` when TickTick says a repeating task is really done.

### End-to-end encryption

Source: OpenTodo `docs/e2ee.md`, `docs/api.md` ("End-to-end encryption") and the Go reference `internal/e2ee/crypt/crypt.go` (checked 2026-11-21, commit 33868fb).

- `GET /api/v1/me` → `encryption: {enabled, keyVersion}`. `GET /api/v1/encryption/keys` (a `full` token is enough) → `{keys: {version, kdf: 1, salt, iterations, wrappedByPassphrase, wrappedByRecovery, ...}}`, base64url without padding.
- Account key: 32 bytes, AES-KW (RFC 3394) wrapped under PBKDF2-HMAC-SHA-256(NFC(passphrase), salt 16 B, 600k–10M iterations), or under HKDF-SHA-256(recovery key, salt = 32 zero bytes, info `opentodo/e2ee/recovery-kek/v1`). Recovery key text: 52 base32 chars (+ a 4-char SHA-256 checksum). Case, spaces and dashes are ignored, and `0/1/8` read as `O/I/B`.
- Value: `"enc:v1:" + base64url(nonce12 || AES-256-GCM ciphertext || tag16)`, AAD `entity \0 id \0 field`. A ciphertext belongs to **one** id and field.
- Encrypted fields used here: task `title`, `description`; project `name`; label `name`. Everything else (`due`, `priority`, `labels` ids, `project_id`, `completed_at`, ...) stays plaintext.
- REST writes of plaintext are accepted and re-encrypted by the next unlocked device ("encrypt on sight"). Their plaintext stays in history until `POST /api/v1/encryption/purge-history`.
- **REST creates assign the id on the server.** With an `Idempotency-Key` it is derived using a secret salt, so the client can't predict it. ticktick-opentodo-sync creates with placeholder content, then PATCHes the ciphertext. If that PATCH fails, it deletes the half-created object. **Race (seen 2026-11-22):** an open, unlocked browser receives the plaintext placeholder from the create, encrypts it on sight and saves it ~0.4 s later, overwriting the content the PATCH just wrote (task history shows the third change set from the browser's device id). The next pass repairs it (`Snapshot.damaged`).
- `description` is the Markdown projection of `notes_doc` (the rich-text doc). A REST write of `description` alone is folded into the doc by the next device that opens the note.
- Label create refuses an empty `name` (`label.name must not be empty`). Task create needs no title.

### Reminders

Separate records, not task fields: `GET /api/v1/reminders` → `{"reminders": [{id, task_id, kind, offset_minutes, fire_at, delivered_at, dismissed_at, created_at, deleted_at}]}`; `POST /api/v1/reminders` `{task_id, kind, offset_minutes}` (accepts `Idempotency-Key`); `PATCH`/`DELETE /api/v1/reminders/{id}`. Kinds: `relative` (minutes from the due time; a date-only due counts from **09:00** in the zone the user's devices last reported), `absolute` (`fire_at`), `deadline`, `arrive`/`leave` (location). The server computes and recomputes `fire_at` of relative reminders when `due` changes, also on a REST `PATCH`. No content fields, so nothing is encrypted. Reminders are personal (per user). Source: OpenTodo `docs/api.md` "Reminders and push" and `docs/sync.md` "Reminders"; OpenTodo's own TickTick importer applies the same −540 shift to all-day triggers (`internal/importer/profile_ticktick.go`).

### Project object

`id, name, color, icon, description, parent_id, position, archived, status, flow_mode, workspace_id, created_at, deleted_at`. There is **no Inbox project object**: the Inbox is `project_id: null`.

## Field mapping (v1)

| Normalized | TickTick | OpenTodo |
|---|---|---|
| title | `title` (stripped) | `title` (stripped) |
| notes | `content` (TEXT) / `desc` (CHECKLIST), CRLF→LF, trailing whitespace stripped | `description` |
| due | `dueDate` + `isAllDay` + `timeZone`. All-day: the date in the task's zone. Timed: local wall time + zone | `due {date, time, tz}`. Floating time takes the default zone |
| priority | `0` none, `1` low, `3` medium, `5` high (`2`→low, `4`→medium on read) | `4` none, `3` low, `2` medium, `1` high |
| tags | `tags` (names, lowercased) | `labels` (IDs → names, lowercased; missing labels created) |
| completed | `status` ∈ {2, -1} | `completed_at != null` |
| project | `projectId` (inbox ID → Inbox) | `project_id` (`null` → Inbox) |
| reminders | `reminders[]` `TRIGGER:` durations; all-day: from midnight (offset = minutes − 540) | `relative` reminders' `offset_minutes` (other kinds left alone) |

The default zone is `TICKTICK_OPENTODO_SYNC_TIMEZONE`, falling back to TickTick's `preference.timeZone` and then to UTC.

TickTick `startDate`: on create it is set to `dueDate`. On update it follows `dueDate` only if it was equal to it or later than the new due. Ranges are otherwise left alone.
