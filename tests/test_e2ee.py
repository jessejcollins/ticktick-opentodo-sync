"""OpenTodo end-to-end encryption: known answers shared with OpenTodo's apps, the client, and
a full sync against an encrypting server (the 2026-11-21 incident as a regression test)."""

from __future__ import annotations

import copy
import itertools
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fakes import FakeTickTick

from ticktick_opentodo_sync import e2ee
from ticktick_opentodo_sync.http import Http
from ticktick_opentodo_sync.opentodo import PLACEHOLDER, EncryptionLockedError, OpenTodo
from ticktick_opentodo_sync.state import State
from ticktick_opentodo_sync.sync import Syncer
from ticktick_opentodo_sync.ticktick import CiphertextWriteError, TickTick

# Known-answer vectors from OpenTodo web/src/lib/e2ee.vectors.json (shared by its Go CLI and
# the browser), so this implementation is checked against theirs, not against itself.
KEY = bytes(range(32))
ACCOUNT_KEY = bytes(range(0x40, 0x60))
PASSPHRASE = "correct horse battery staple"
RECOVERY_TEXT = "QCAY-FA4E-QWDI-PCEJ-RKFY-ZDMO-R6IJ-DEUT-SSKZ-NF4Y-TGNJ-XHE5-T2PQ-QLMG"
RECORD = e2ee.KeyRecord(
    version=1,
    kdf=1,
    salt=e2ee._b64e(bytes.fromhex("101112131415161718191a1b1c1d1e1f")),
    iterations=1000,
    wrapped_by_passphrase="IhPkjEwMwQjIq6ZCtz4_Qy82U3YOqmvFEdWDSV6W3c8-ToyYBwgXbA",
    wrapped_by_recovery="LCFihcp0aqAZBq7Cl9C3ZQbzk03iDPjbEPS6-nUmlKdywd1DzgriFw",
)
CONTENT_VECTORS = [
    (
        "a0a1a2a3a4a5a6a7a8a9aaab",
        "task",
        "t1",
        "title",
        "Dentist",
        "enc:v1:oKGio6Slpqeoqaqron0SWSy4dh8xioWkMvJuPN5GjNypnM8",
    ),
    (
        "b0b1b2b3b4b5b6b7b8b9babb",
        "task",
        "6b9f2c1e-3d4a-4f8b-9c2d-1e3f4a5b6c7d",
        "description",
        "Ask about the crown — café, 🦷",
        "enc:v1:sLGys7S1tre4ubq72CYxi42v1Coz2OPKqH3rsOtLJ_L3rhsVPa_kMvWu0uaa7ZREpBoJLdRcqUDYxZ_ZmHSJ",
    ),
    (
        "c0c1c2c3c4c5c6c7c8c9cacb",
        "label",
        "l1",
        "name",
        "",
        "enc:v1:wMHCw8TFxsfIycrLd6Fz1u8Lz0wn5RpDcBwXKA",
    ),
]


@pytest.mark.parametrize(("nonce", "entity", "eid", "field", "plaintext", "value"), CONTENT_VECTORS)
def test_content_vectors(
    nonce: str, entity: str, eid: str, field: str, plaintext: str, value: str
) -> None:
    key = e2ee.AccountKey(KEY)
    assert key.seal(entity, eid, field, plaintext, nonce=bytes.fromhex(nonce)) == value
    assert key.open(entity, eid, field, value) == plaintext


def test_open_is_bound_to_entity_id_and_field() -> None:
    key = e2ee.AccountKey(KEY)
    value = CONTENT_VECTORS[0][-1]
    for entity, eid, field in [
        ("label", "t1", "title"),
        ("task", "t2", "title"),
        ("task", "t1", "x"),
    ]:
        with pytest.raises(e2ee.DecryptError):
            key.open(entity, eid, field, value)
    with pytest.raises(e2ee.DecryptError):
        e2ee.AccountKey(ACCOUNT_KEY).open("task", "t1", "title", value)


def _roundtrip(key: e2ee.AccountKey) -> str:
    return key.open("task", "x", "title", key.seal("task", "x", "title", "hello"))


def test_unlock_with_passphrase_and_recovery_key() -> None:
    by_pass = e2ee.unlock_with_passphrase(RECORD, PASSPHRASE, min_iterations=1000)
    by_recovery = e2ee.unlock_with_recovery_key(RECORD, RECOVERY_TEXT.lower().replace("-", " "))
    sealed = e2ee.AccountKey(ACCOUNT_KEY).seal("task", "x", "title", "hello")
    assert by_pass.open("task", "x", "title", sealed) == "hello"
    assert by_recovery.open("task", "x", "title", sealed) == "hello"
    assert _roundtrip(by_pass) == "hello"
    assert "40414243" not in repr(by_pass)


def test_unlock_failures() -> None:
    with pytest.raises(e2ee.WrongSecretError):
        e2ee.unlock_with_passphrase(RECORD, "wrong", min_iterations=1000)
    with pytest.raises(e2ee.E2EEError, match="iterations"):
        e2ee.unlock_with_passphrase(RECORD, PASSPHRASE)  # 1000 < the real minimum
    with pytest.raises(e2ee.E2EEError, match="typos"):
        e2ee.unlock_with_recovery_key(RECORD, RECOVERY_TEXT[:-1] + "A")


def test_ticktick_refuses_ciphertext_writes() -> None:
    tt = TickTick(Http("t", "https://tt.example", "x", min_interval=0))
    with pytest.raises(CiphertextWriteError, match=r"body\.tags\[1\]"):
        tt.create_task({"title": "ok", "tags": ["a", CONTENT_VECTORS[0][-1]]})
    with pytest.raises(CiphertextWriteError):
        tt.create_project("enc:v1:AAAA")


# --- an encrypting OpenTodo server -----------------------------------------------------------

OT = "https://ot.example"
CONTENT = {
    "tasks": ("task", ("title", "description")),
    "projects": ("project", ("name",)),
    "labels": ("label", ("name",)),
    "reminders": ("reminder", ()),
}


class EncryptedServer:
    """In-memory OpenTodo with E2EE on: stores whatever it is sent, never decrypts.

    :meth:`browser` plays an unlocked browser: it encrypts plaintext on sight and, to be
    hostile, re-encrypts everything else with fresh nonces.
    """

    def __init__(self, router: respx.MockRouter) -> None:
        self.key = e2ee.AccountKey(ACCOUNT_KEY)
        self.data: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in CONTENT}
        self.ids = itertools.count(1)
        self.writes: list[tuple[str, str, Any]] = []
        self.fail_patch = False
        router.get(f"{OT}/api/v1/me").mock(
            return_value=httpx.Response(
                200, json={"encryption": {"enabled": True, "keyVersion": 1}}
            )
        )
        record = {
            "version": 1,
            "kdf": 1,
            "salt": RECORD.salt,
            "iterations": RECORD.iterations,
            "wrappedByPassphrase": RECORD.wrapped_by_passphrase,
            "wrappedByRecovery": RECORD.wrapped_by_recovery,
        }
        router.get(f"{OT}/api/v1/encryption/keys").mock(
            return_value=httpx.Response(200, json={"keys": record})
        )
        colls = "|".join(CONTENT)
        pattern = re.compile(rf"{OT}/api/v1/({colls})(?:/([^/?]+))?(?:\?.*)?$")
        router.route(url__regex=pattern.pattern).mock(side_effect=self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        m = re.match(rf"{OT}/api/v1/(\w+)(?:/([^/?]+))?", str(request.url))
        assert m
        coll, oid = m.group(1), m.group(2)
        store = self.data[coll]
        if request.method == "GET" and oid is None:
            return httpx.Response(200, json={coll: [copy.deepcopy(x) for x in store.values()]})
        body = json.loads(request.content) if request.content else {}
        self.writes.append((request.method, coll, body))
        if request.method == "POST":
            new_id = f"{coll[0]}{next(self.ids)}"
            store[new_id] = {"id": new_id, "deleted_at": None} | body
            if coll == "tasks":
                store[new_id] = {"labels": [], "completed_at": None, "priority": 4} | store[new_id]
            return httpx.Response(201, json=store[new_id])
        assert oid in store
        if request.method == "PATCH":
            if self.fail_patch:
                return httpx.Response(400, json={"error": {"code": "bad_request"}})
            store[oid] |= body
            return httpx.Response(200, json=store[oid])
        if request.method == "DELETE":
            store[oid]["deleted_at"] = 1
            return httpx.Response(204)
        raise AssertionError(request)

    def browser(self) -> None:
        for coll, (entity, fields) in CONTENT.items():
            for oid, obj in self.data[coll].items():
                for f in fields:
                    v = obj.get(f)
                    if isinstance(v, str):
                        if e2ee.is_ciphertext(v):
                            v = self.key.open(entity, oid, f, v)
                        obj[f] = self.key.seal(entity, oid, f, v)

    def plain(self, coll: str) -> list[dict[str, Any]]:
        entity, fields = CONTENT[coll]
        out = []
        for oid, obj in self.data[coll].items():
            o = dict(obj)
            for f in fields:
                if e2ee.is_ciphertext(o.get(f)):
                    o[f] = self.key.open(entity, oid, f, o[f])
            out.append(o)
        return out

    def all_values(self) -> Iterator[Any]:
        for coll, (_, fields) in CONTENT.items():
            for obj in self.data[coll].values():
                yield from (obj.get(f) for f in fields)


@pytest.fixture
def server() -> Iterator[EncryptedServer]:
    with respx.mock(assert_all_called=False) as router:
        yield EncryptedServer(router)


def client(**secrets: str) -> OpenTodo:
    return OpenTodo(
        Http("OpenTodo", OT, "tok", min_interval=0),
        unlock_passphrase=lambda r, p: e2ee.unlock_with_passphrase(r, p, min_iterations=1000),
        **secrets,
    )


def test_locked_without_secret(server: EncryptedServer) -> None:
    with pytest.raises(EncryptionLockedError, match="OPENTODO_ENCRYPTION_PASSPHRASE"):
        client().begin_pass()
    with pytest.raises(EncryptionLockedError, match="not correct"):
        client(passphrase="nope").begin_pass()


def test_create_never_sends_plaintext(server: EncryptedServer) -> None:
    ot = client(recovery_key=RECOVERY_TEXT)
    ot.begin_pass()
    task = ot.create_task(
        {"title": "Secret", "description": "notes", "priority": 1}, idempotency_key="k"
    )
    assert (task["title"], task["description"]) == ("Secret", "notes")
    label = ot.create_label("home")
    assert label["name"] == "home"
    sent = json.dumps([w[2] for w in server.writes])
    assert "Secret" not in sent and "notes" not in sent and "home" not in sent
    assert all(e2ee.is_ciphertext(v) for v in server.all_values())
    # Ciphertext is bound to the server-assigned id.
    t = server.data["tasks"][task["id"]]
    assert server.key.open("task", task["id"], "title", t["title"]) == "Secret"


def test_half_created_task_is_removed_and_ignored(server: EncryptedServer) -> None:
    ot = client(passphrase=PASSPHRASE)
    ot.begin_pass()
    server.fail_patch = True
    with pytest.raises(Exception, match="400"):
        ot.create_task({"title": "Secret"}, idempotency_key="k")
    (orphan,) = server.data["tasks"].values()
    assert orphan["title"] == PLACEHOLDER and orphan["deleted_at"] == 1


def test_unreadable_values_are_marked(server: EncryptedServer) -> None:
    server.data["tasks"]["t9"] = {
        "id": "t9",
        "title": server.key.seal("task", "OTHER", "title", "x"),
    }
    server.data["tasks"]["t8"] = {"id": "t8", "title": "typed via email"}  # plaintext: fine
    ot = client(passphrase=PASSPHRASE)
    ot.begin_pass()
    by_id = {t["id"]: t for t in ot.tasks()}
    assert by_id["t9"].get("_ticktick_opentodo_sync_unreadable")
    assert by_id["t8"]["title"] == "typed via email"


def test_full_sync_against_encrypted_opentodo(server: EncryptedServer, tmp_path: Path) -> None:
    """The incident: seed, browser re-encrypts, next pass must see no OpenTodo change."""
    tt = FakeTickTick()
    work = tt.add_project("Work")
    tt.add_task("Report", work, content="Q3 numbers", priority=5, tags=["Office"])
    tt.add_task("Inbox thing")
    state = State(tmp_path / "s.db")
    syncer = Syncer(tt, client(passphrase=PASSPHRASE), state)

    res = syncer.run(dry_run=False)
    assert res.ok and res.ot.created == 2, res.summary()
    assert {t["title"] for t in server.plain("tasks")} == {"Report", "Inbox thing"}
    assert [p["name"] for p in server.plain("projects")] == ["Work"]
    assert [x["name"] for x in server.plain("labels")] == ["office"]
    assert all(e2ee.is_ciphertext(v) for v in server.all_values())

    server.browser()
    tt_writes = len(tt.writes)
    res = syncer.run(dry_run=False)
    assert not res.changed and res.ok, res.summary()
    assert len(tt.writes) == tt_writes

    # A real edit in OpenTodo still comes through, decrypted.
    report = next(t for t in server.plain("tasks") if t["title"] == "Report")
    server.data["tasks"][report["id"]]["title"] = server.key.seal(
        "task", report["id"], "title", "Report v2"
    )
    res = syncer.run(dry_run=False)
    assert res.tt.updated == 1, res.summary()
    assert any(t["title"] == "Report v2" for t in tt.tasks.values())
    assert not any(e2ee.is_ciphertext(v) for t in tt.tasks.values() for v in t.values())
    state.close()


def test_browser_saving_the_placeholder_is_repaired(
    server: EncryptedServer, tmp_path: Path
) -> None:
    """2026-11-22: an open browser saved its sealed copy of the create placeholder over
    the content PATCHed a moment earlier. The next pass rewrites OpenTodo from TickTick."""
    tt = FakeTickTick()
    tt.add_task("Clean the gutters", content="the good one")
    state = State(tmp_path / "s.db")
    syncer = Syncer(tt, client(passphrase=PASSPHRASE), state)
    assert syncer.run(dry_run=False).ot.created == 1

    ((oid, task),) = server.data["tasks"].items()
    for f in ("title", "description"):
        task[f] = server.key.seal("task", oid, f, PLACEHOLDER)
    # A create interrupted before it was paired: left alone, never copied to TickTick.
    server.data["tasks"]["t99"] = {
        "id": "t99",
        "title": PLACEHOLDER,
        "labels": [],
        "completed_at": None,
        "deleted_at": None,
    }
    tt_writes = len(tt.writes)

    res = syncer.run(dry_run=False)
    assert res.ok and res.ot.updated == 1 and not res.ot.created, res.summary()
    assert len(tt.writes) == tt_writes
    plain = {t["id"]: t for t in server.plain("tasks")}
    assert (plain[oid]["title"], plain[oid]["description"]) == (
        "Clean the gutters",
        "the good one",
    )
    assert e2ee.is_ciphertext(server.data["tasks"][oid]["title"])

    res = syncer.run(dry_run=False)
    assert not res.changed and res.ok, res.summary()
    assert len(tt.writes) == tt_writes
    state.close()


def test_ticktick_refuses_the_placeholder() -> None:
    tt = TickTick(Http("t", "https://tt.example", "x", min_interval=0))
    with pytest.raises(CiphertextWriteError, match="placeholder"):
        tt.update_task("x", {"id": "x", "title": PLACEHOLDER})
