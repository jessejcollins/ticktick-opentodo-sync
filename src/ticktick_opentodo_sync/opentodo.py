"""OpenTodo REST API client (``/api/v1``, bearer token). Endpoints: docs/api-notes.md.

With end-to-end encryption on, this client is the only place that sees ciphertext: reads
come back decrypted and writes are encrypted, so everything above it works on plaintext.
A value that cannot be decrypted marks its object with :data:`UNREADABLE` instead.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any, cast

from . import e2ee
from .http import ApiError, Http
from .models import JSON

log = logging.getLogger(__name__)

UNREADABLE = "_ticktick_opentodo_sync_unreadable"
"""Set (True) on a returned object whose content could not be decrypted."""

PLACEHOLDER = "ticktick-opentodo-sync: encrypting…"
"""Content of a just-created object until the PATCH with its ciphertext lands.

Ciphertext is bound to the object's id, which the server assigns on create.
"""

REMINDERS = "reminders"
"""Task key of this client's own (not an OpenTodo field): sorted ``offset_minutes`` of the
task's live *relative* reminders. Reads carry it; creates and updates that carry it make the
task's relative reminders match. Other kinds (absolute, deadline, location) are left alone.
"""

TASK_CONTENT = ("title", "description")
NAME_CONTENT = ("name",)


class EncryptionLockedError(Exception):
    """OpenTodo is encrypted and this client has no usable key."""


class EncryptionExpectedError(Exception):
    """A passphrase or recovery key is set, but OpenTodo doesn't report encryption on."""


class OpenTodo:
    def __init__(
        self,
        http: Http,
        *,
        passphrase: str | None = None,
        recovery_key: str | None = None,
        unlock_passphrase: Callable[[e2ee.KeyRecord, str], e2ee.AccountKey] = (
            e2ee.unlock_with_passphrase
        ),
    ) -> None:
        self.http = http
        self._passphrase = passphrase
        self._recovery_key = recovery_key
        self._expects_encryption = bool(passphrase or recovery_key)
        """A secret is set: content is never written in plaintext, whatever the server says."""
        self._unlock_passphrase = unlock_passphrase
        self._key: e2ee.AccountKey | None = None
        self._key_version: int | None = None
        self.encrypted = False
        self._reminders: dict[str, list[JSON]] = {}
        """Task id -> its live relative reminders, as of :meth:`tasks` plus our writes."""

    # --- encryption ---------------------------------------------------------------------

    def begin_pass(self) -> None:
        """Check the account's encryption and unlock (or re-unlock) the key if needed.

        With a passphrase or recovery key set, encryption is expected: unless OpenTodo reports
        it on, the pass stops. Otherwise a server answering "off" (or a ``/me`` that changed
        shape) would get every write in plaintext, decrypted with the key from earlier passes.
        """
        me = self.http.request("GET", "/api/v1/me")
        if not isinstance(me, dict):
            # Never read "encryption off" from a response we don't understand.
            raise TypeError(f"unexpected GET /api/v1/me response: {type(me).__name__}")
        enc = me.get("encryption")
        version: object = None
        if enc is None:
            enabled = False  # a server without end-to-end encryption
        elif isinstance(enc, dict) and isinstance(enc.get("enabled"), bool):
            enabled, version = enc["enabled"], enc.get("keyVersion")
        else:
            raise TypeError(f"unexpected GET /api/v1/me encryption: {enc!r}")
        if not enabled and self._expects_encryption:
            raise EncryptionExpectedError(
                "OPENTODO_ENCRYPTION_PASSPHRASE or OPENTODO_ENCRYPTION_RECOVERY_KEY is set, but "
                "OpenTodo doesn't report end-to-end encryption on, so nothing is written. If "
                "you turned encryption off, remove the secret from .env"
            )
        self.encrypted = enabled
        if not enabled:
            return
        if self._key is not None and self._key_version == version:
            return
        self._key = None  # stale (or never unlocked): unusable until the unlock below succeeds
        if not (self._passphrase or self._recovery_key):
            raise EncryptionLockedError(
                "OpenTodo has end-to-end encryption on: set OPENTODO_ENCRYPTION_PASSPHRASE "
                "or OPENTODO_ENCRYPTION_RECOVERY_KEY so the sync can read and write it"
            )
        data = self.http.request("GET", "/api/v1/encryption/keys")
        record = e2ee.KeyRecord.from_json(_obj(data).get("keys") or {})
        try:
            if self._recovery_key:
                key = e2ee.unlock_with_recovery_key(record, self._recovery_key)
            else:
                assert self._passphrase
                key = self._unlock_passphrase(record, self._passphrase)
        except e2ee.E2EEError as exc:
            raise EncryptionLockedError(f"cannot unlock OpenTodo encryption: {exc}") from None
        self._key, self._key_version = key, record.version
        log.info("OpenTodo end-to-end encryption unlocked (key record v%s)", record.version)

    def _open(self, entity: str, obj: JSON, fields: tuple[str, ...]) -> JSON:
        for f in fields:
            value = obj.get(f)
            if not e2ee.is_ciphertext(value):
                continue  # plaintext (not yet encrypted on sight) or absent
            if self._key is None:
                obj[UNREADABLE] = True
                continue
            try:
                obj[f] = self._key.open(entity, str(obj.get("id")), f, cast(str, value))
            except e2ee.DecryptError:
                obj[UNREADABLE] = True
        return obj

    def _sealing_key(self) -> e2ee.AccountKey | None:
        """The key writes are encrypted with; None only for an account without encryption.

        Never None while a secret is set, even if :meth:`begin_pass` failed or wasn't called.
        """
        if self.encrypted and self._key is not None:
            return self._key
        if self.encrypted or self._expects_encryption:
            raise EncryptionLockedError("OpenTodo encryption is not unlocked; nothing is written")
        return None

    def _seal(self, entity: str, obj_id: str, body: JSON, fields: tuple[str, ...]) -> JSON:
        key = self._sealing_key()
        if key is None:
            return body
        out = dict(body)
        for f in fields:
            if isinstance(out.get(f), str):
                out[f] = key.seal(entity, obj_id, f, out[f])
        return out

    def _create_sealed(
        self,
        entity: str,
        collection: str,
        body: JSON,
        fields: tuple[str, ...],
        headers: dict[str, str] | None = None,
    ) -> JSON:
        """Create with placeholder content, then PATCH the content sealed for the new id."""
        path = f"/api/v1/{collection}"
        if self._sealing_key() is None:
            return self._open(
                entity, _obj(self.http.request("POST", path, json=body, headers=headers)), fields
            )
        content = {f: body[f] for f in fields if f in body}
        created = _obj(
            self.http.request(
                "POST",
                path,
                json=body | dict.fromkeys(content, PLACEHOLDER),
                headers=headers,
            )
        )
        new_id = str(created["id"])
        try:
            resp = self.http.request(
                "PATCH", f"{path}/{new_id}", json=self._seal(entity, new_id, content, fields)
            )
        except Exception:
            try:
                self.http.request("DELETE", f"{path}/{new_id}", not_found_ok=True)
            except ApiError as exc:
                log.error(  # noqa: TRY400
                    "could not remove half-created OpenTodo %s %s (title %r): %s",
                    entity,
                    new_id,
                    PLACEHOLDER,
                    exc,
                )
            raise
        return self._open(entity, _obj(resp), fields)

    # --- API ----------------------------------------------------------------------------

    def projects(self) -> list[JSON]:
        items = _items(self.http.request("GET", "/api/v1/projects"), "projects")
        return [self._open("project", p, NAME_CONTENT) for p in items]

    def create_project(self, name: str) -> JSON:
        return self._create_sealed(
            "project", "projects", {"name": name}, NAME_CONTENT, _idempotency_key()
        )

    def rename_project(self, project_id: str, name: str) -> JSON:
        sealed = self._seal("project", project_id, {"name": name}, NAME_CONTENT)
        resp = self.http.request("PATCH", f"/api/v1/projects/{project_id}", json=sealed)
        return self._open("project", _obj(resp), NAME_CONTENT)

    def archive_project(self, project_id: str) -> JSON:
        """Archive a project (with its sub-projects). ``archived`` must be sent on its own."""
        resp = self.http.request("PATCH", f"/api/v1/projects/{project_id}", json={"archived": True})
        return self._open("project", _obj(resp), NAME_CONTENT)

    def restore_project(self, project_id: str) -> JSON:
        """Bring a project back from the Trash; its tasks, only hidden, come back with it."""
        resp = self.http.request(
            "PATCH", f"/api/v1/projects/{project_id}", json={"deleted_at": None}
        )
        return self._open("project", _obj(resp), NAME_CONTENT)

    def labels(self) -> list[JSON]:
        items = _items(self.http.request("GET", "/api/v1/labels"), "labels")
        return [self._open("label", x, NAME_CONTENT) for x in items]

    def create_label(self, name: str) -> JSON:
        return self._create_sealed(
            "label", "labels", {"name": name}, NAME_CONTENT, _idempotency_key()
        )

    def tasks(self) -> list[JSON]:
        """Open, completed and soft-deleted (``deleted_at`` set) tasks, with :data:`REMINDERS`."""
        data = self.http.request("GET", "/api/v1/tasks", params={"include_deleted": "true"})
        self._load_reminders()
        return [
            self._with_reminders(self._open("task", t, TASK_CONTENT)) for t in _items(data, "tasks")
        ]

    def create_task(self, body: JSON, *, idempotency_key: str) -> JSON:
        body, wanted = _split_reminders(body)
        headers = {"Idempotency-Key": idempotency_key}
        created = self._create_sealed("task", "tasks", body, TASK_CONTENT, headers)
        if wanted:
            try:
                self._set_reminders(str(created["id"]), wanted)
            except Exception as exc:
                # The task exists now: failing here would create it again next pass.
                log.error("OpenTodo task %s: reminders not added: %s", created["id"], exc)  # noqa: TRY400
        return self._with_reminders(created)

    def update_task(self, task_id: str, body: JSON) -> JSON:
        body, wanted = _split_reminders(body)
        sealed = self._seal("task", task_id, body, TASK_CONTENT)
        # The task first: relative reminders fire from its (possibly new) due.
        resp = self.http.request("PATCH", f"/api/v1/tasks/{task_id}", json=sealed)
        if wanted is not None:
            self._set_reminders(task_id, wanted)
        return self._with_reminders(self._open("task", _obj(resp), TASK_CONTENT))

    # --- reminders ----------------------------------------------------------------------

    def _load_reminders(self) -> None:
        data = self.http.request("GET", "/api/v1/reminders")
        self._reminders = {}
        for r in _items(data, "reminders"):
            if (
                r.get("deleted_at") is None
                and r.get("kind") == "relative"
                and r.get("task_id")
                and isinstance(r.get("offset_minutes"), int)
            ):
                self._reminders.setdefault(str(r["task_id"]), []).append(r)

    def _with_reminders(self, task: JSON) -> JSON:
        mine = self._reminders.get(str(task.get("id")), [])
        task[REMINDERS] = sorted(int(r["offset_minutes"]) for r in mine)
        return task

    def _set_reminders(self, task_id: str, wanted: list[int]) -> None:
        """Make the task's relative reminders exactly ``wanted`` (one per offset)."""
        current = self._reminders.setdefault(task_id, [])  # updated as each write lands
        missing = list(wanted)
        for r in list(current):
            if r["offset_minutes"] in missing:
                missing.remove(r["offset_minutes"])
            else:
                self.http.request("DELETE", f"/api/v1/reminders/{r['id']}", not_found_ok=True)
                current.remove(r)
        for minutes in missing:
            created = self.http.request(
                "POST",
                "/api/v1/reminders",
                json={"task_id": task_id, "kind": "relative", "offset_minutes": minutes},
                headers=_idempotency_key(),
            )
            current.append(_obj(created) | {"offset_minutes": minutes})

    def complete_occurrence(self, task_id: str, date: str, completed_at: int | None) -> JSON:
        """Complete one occurrence of a repeating task; the current one moves the series on."""
        body = {} if completed_at is None else {"completed_at": completed_at}
        resp = self.http.request(
            "POST", f"/api/v1/tasks/{task_id}/occurrences/{date}/complete", json=body
        )
        return self._open("task", _obj(resp), TASK_CONTENT)

    def log_habit(self, habit_id: str, day: str, state: str, timezone: str | None) -> JSON:
        """Log a habit's day as the app does (``done``, ``tiny``, ``skipped`` or ``clear``).

        The current occurrence completes and moves the habit; an earlier day gets its record.
        Returns ``{"habit": {..., "current_date"}, "days": [...]}``.
        """
        body: JSON = {"state": state, "date": day}
        if timezone:
            body["timezone"] = timezone
        return _obj(self.http.request("POST", f"/api/v1/habits/{habit_id}/log", json=body))

    def delete_task(self, task_id: str) -> None:
        """Soft delete (to the Trash)."""
        self.http.request("DELETE", f"/api/v1/tasks/{task_id}", not_found_ok=True)


def _idempotency_key() -> dict[str, str]:
    """A fresh key for one create: OpenTodo dedupes that call's retries (see Http.request)."""
    return {"Idempotency-Key": f"ticktick-opentodo-sync-{uuid.uuid4()}"}


def _split_reminders(body: JSON) -> tuple[JSON, list[int] | None]:
    if REMINDERS not in body:
        return body, None
    rest = dict(body)
    wanted = rest.pop(REMINDERS)
    return rest, sorted({int(m) for m in wanted or ()})


def _items(data: Any, key: str) -> list[JSON]:
    """The ``key`` list of a response; anything else raises (never read as "none")."""
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise TypeError(f"unexpected OpenTodo response: no {key!r} list")
    return [x for x in items if isinstance(x, dict)]


def _obj(data: Any) -> JSON:
    return cast(JSON, data) if isinstance(data, dict) else {}
