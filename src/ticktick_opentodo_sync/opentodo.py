"""OpenTodo REST API client (``/api/v1``, bearer token). Endpoints: docs/api-notes.md.

With end-to-end encryption on, this client is the only place that sees ciphertext: reads
come back decrypted and writes are encrypted, so everything above it works on plaintext.
A value that cannot be decrypted marks its object with :data:`UNREADABLE` instead.
"""

from __future__ import annotations

import logging
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

TASK_CONTENT = ("title", "description")
NAME_CONTENT = ("name",)


class EncryptionLockedError(Exception):
    """OpenTodo is encrypted and this client has no usable key."""


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
        self._unlock_passphrase = unlock_passphrase
        self._key: e2ee.AccountKey | None = None
        self._key_version: int | None = None
        self.encrypted = False

    # --- encryption ---------------------------------------------------------------------

    def begin_pass(self) -> None:
        """Check the account's encryption and unlock (or re-unlock) the key if needed."""
        me = self.http.request("GET", "/api/v1/me")
        enc = me.get("encryption") if isinstance(me, dict) else None
        enc = enc if isinstance(enc, dict) else {}
        self.encrypted = bool(enc.get("enabled"))
        if not self.encrypted:
            return
        version = enc.get("keyVersion")
        if self._key is not None and self._key_version == version:
            return
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

    def _seal(self, entity: str, obj_id: str, body: JSON, fields: tuple[str, ...]) -> JSON:
        if not self.encrypted:
            return body
        if self._key is None:
            raise EncryptionLockedError("OpenTodo is encrypted but not unlocked")
        out = dict(body)
        for f in fields:
            if isinstance(out.get(f), str):
                out[f] = self._key.seal(entity, obj_id, f, out[f])
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
        if not self.encrypted:
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
        return self._create_sealed("project", "projects", {"name": name}, NAME_CONTENT)

    def labels(self) -> list[JSON]:
        items = _items(self.http.request("GET", "/api/v1/labels"), "labels")
        return [self._open("label", x, NAME_CONTENT) for x in items]

    def create_label(self, name: str) -> JSON:
        return self._create_sealed("label", "labels", {"name": name}, NAME_CONTENT)

    def tasks(self) -> list[JSON]:
        """Open, completed and soft-deleted (``deleted_at`` set) tasks."""
        data = self.http.request("GET", "/api/v1/tasks", params={"include_deleted": "true"})
        return [self._open("task", t, TASK_CONTENT) for t in _items(data, "tasks")]

    def create_task(self, body: JSON, *, idempotency_key: str) -> JSON:
        headers = {"Idempotency-Key": idempotency_key}
        return self._create_sealed("task", "tasks", body, TASK_CONTENT, headers)

    def update_task(self, task_id: str, body: JSON) -> JSON:
        sealed = self._seal("task", task_id, body, TASK_CONTENT)
        resp = self.http.request("PATCH", f"/api/v1/tasks/{task_id}", json=sealed)
        return self._open("task", _obj(resp), TASK_CONTENT)

    def delete_task(self, task_id: str) -> None:
        """Soft delete (to the Trash)."""
        self.http.request("DELETE", f"/api/v1/tasks/{task_id}", not_found_ok=True)


def _items(data: Any, key: str) -> list[JSON]:
    items = data.get(key) if isinstance(data, dict) else None
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def _obj(data: Any) -> JSON:
    return cast(JSON, data) if isinstance(data, dict) else {}
