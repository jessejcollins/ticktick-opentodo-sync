"""HTTP-level tests for both clients (respx)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ticktick_opentodo_sync.http import ApiError, Http
from ticktick_opentodo_sync.opentodo import OpenTodo
from ticktick_opentodo_sync.ticktick import TickTick

TT = "https://api.ticktick.com"
OT = "https://ot.example"


def http(base: str, sleeps: list[float] | None = None) -> Http:
    return Http(
        "test",
        base,
        "secret",
        min_interval=0,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


@respx.mock
def test_bearer_auth_and_json() -> None:
    route = respx.get(f"{TT}/open/v1/project").mock(
        return_value=httpx.Response(200, json=[{"id": "p"}])
    )
    assert TickTick(http(TT)).projects() == [{"id": "p"}]
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"


@respx.mock
def test_retry_on_429_honours_retry_after() -> None:
    sleeps: list[float] = []
    respx.get(f"{TT}/open/v1/project").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, json=[]),
        ]
    )
    assert TickTick(http(TT, sleeps)).projects() == []
    assert sleeps == [7.0, 2.0]


@respx.mock
def test_gives_up_and_raises() -> None:
    respx.get(f"{TT}/open/v1/project").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ApiError, match="HTTP 500"):
        TickTick(http(TT)).projects()


@respx.mock
def test_create_or_completion_not_repeated_after_an_unknown_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repeated create whose answer was lost made a duplicate, and a repeated completion
    moved a repeating task on twice (review #7)."""
    tt = TickTick(http(TT))
    create = respx.post(f"{TT}/open/v1/task")
    unknown: list[Exception | httpx.Response] = [
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("dropped"),
        httpx.Response(500),
        httpx.Response(502),
        httpx.Response(503),  # without Retry-After
        httpx.Response(504),
    ]
    for answer in unknown:
        before = create.call_count
        create.mock(side_effect=[answer, httpx.Response(200, json={"id": "t"})])
        with pytest.raises((httpx.TransportError, ApiError)):
            tt.create_task({"title": "x", "projectId": "p"})
        assert create.call_count == before + 1
    assert "may have gone through" in caplog.text

    complete = respx.post(f"{TT}/open/v1/project/p/task/t/complete").mock(
        side_effect=[httpx.Response(504), httpx.Response(200)]
    )
    with pytest.raises(ApiError, match="504"):
        tt.complete_task("p", "t")
    assert complete.call_count == 1


@respx.mock
def test_create_repeated_when_it_cannot_have_gone_through() -> None:
    tt = TickTick(http(TT))
    create = respx.post(f"{TT}/open/v1/task")
    not_acted_on: list[Exception | httpx.Response] = [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("no route"),
        httpx.Response(429),
        httpx.Response(503, headers={"Retry-After": "1"}),
    ]
    for answer in not_acted_on:
        create.mock(side_effect=[answer, httpx.Response(200, json={"id": "t"})])
        assert tt.create_task({"title": "x", "projectId": "p"}) == {"id": "t"}


@respx.mock
def test_repeatable_requests_are_retried() -> None:
    """Updates, reads sent as POST, and creates carrying an Idempotency-Key."""
    tt = TickTick(http(TT))
    upd = respx.post(f"{TT}/open/v1/task/t1").mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(502),
            httpx.Response(200, json={"id": "t1"}),
        ]
    )
    assert tt.update_task("t1", {"title": "x"}) == {"id": "t1"}
    assert upd.call_count == 3

    pref = respx.post(f"{TT}/open/v1/preference").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"timeZone": "UTC"})]
    )
    assert tt.preference_timezone() == "UTC" and pref.call_count == 2

    create = respx.post(f"{OT}/api/v1/projects").mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(201, json={"id": "p1", "name": "Work"}),
        ]
    )
    assert OpenTodo(http(OT)).create_project("Work")["id"] == "p1"
    # The same key both times, so OpenTodo dedupes the retry.
    assert len({c.request.headers["Idempotency-Key"] for c in create.calls}) == 1


@respx.mock
def test_client_error_is_not_retried() -> None:
    route = respx.post(f"{OT}/api/v1/tasks").mock(return_value=httpx.Response(400, json={}))
    with pytest.raises(ApiError):
        OpenTodo(http(OT)).create_task({"title": "x"}, idempotency_key="k")
    assert route.call_count == 1


@respx.mock
def test_ticktick_task_endpoints() -> None:
    tt = TickTick(http(TT))
    respx.get(f"{TT}/open/v1/project/p/task/missing").mock(return_value=httpx.Response(404))
    assert tt.get_task("p", "missing") is None

    upd = respx.post(f"{TT}/open/v1/task/t1").mock(
        return_value=httpx.Response(200, json={"id": "t1"})
    )
    tt.update_task("t1", {"title": "x", "projectId": "p"})
    assert json.loads(upd.calls[0].request.content) == {"title": "x", "projectId": "p", "id": "t1"}

    done = respx.post(f"{TT}/open/v1/project/p/task/t1/complete").mock(
        return_value=httpx.Response(200)
    )
    tt.complete_task("p", "t1")
    assert done.called

    move = respx.post(f"{TT}/open/v1/task/move").mock(return_value=httpx.Response(200, json=[]))
    tt.move_task("t1", "a", "b")
    assert json.loads(move.calls[0].request.content) == [
        {"fromProjectId": "a", "toProjectId": "b", "taskId": "t1"}
    ]

    respx.post(f"{TT}/open/v1/preference").mock(
        return_value=httpx.Response(200, json={"timeZone": "Europe/Oslo"})
    )
    assert tt.preference_timezone() == "Europe/Oslo"


@respx.mock
def test_opentodo_endpoints() -> None:
    ot = OpenTodo(http(OT))
    lst = respx.get(f"{OT}/api/v1/tasks").mock(
        return_value=httpx.Response(200, json={"tasks": [{"id": "a"}]})
    )
    respx.get(f"{OT}/api/v1/reminders").mock(
        return_value=httpx.Response(200, json={"reminders": []})
    )
    assert ot.tasks() == [{"id": "a", "reminders": []}]
    assert lst.calls[0].request.url.params["include_deleted"] == "true"

    create = respx.post(f"{OT}/api/v1/tasks").mock(
        return_value=httpx.Response(201, json={"id": "n"})
    )
    assert ot.create_task({"title": "x"}, idempotency_key="key-1") == {"id": "n", "reminders": []}
    assert create.calls[0].request.headers["Idempotency-Key"] == "key-1"

    patch = respx.patch(f"{OT}/api/v1/tasks/n").mock(
        return_value=httpx.Response(200, json={"id": "n"})
    )
    ot.update_task("n", {"completed_at": None})
    assert json.loads(patch.calls[0].request.content) == {"completed_at": None}

    respx.delete(f"{OT}/api/v1/tasks/n").mock(return_value=httpx.Response(204))
    ot.delete_task("n")

    respx.get(f"{OT}/api/v1/labels").mock(
        return_value=httpx.Response(200, json={"labels": [{"id": "l"}]})
    )
    assert ot.labels() == [{"id": "l"}]


@respx.mock
def test_opentodo_reminders() -> None:
    """Only live relative reminders are read and reconciled; other kinds are left alone."""
    ot = OpenTodo(http(OT))
    respx.get(f"{OT}/api/v1/tasks").mock(
        return_value=httpx.Response(200, json={"tasks": [{"id": "t"}, {"id": "u"}]})
    )
    rows = [
        {"id": "r1", "task_id": "t", "kind": "relative", "offset_minutes": 0, "deleted_at": None},
        {"id": "r2", "task_id": "t", "kind": "relative", "offset_minutes": -30, "deleted_at": None},
        {"id": "r3", "task_id": "t", "kind": "relative", "offset_minutes": -60, "deleted_at": 5},
        {"id": "r4", "task_id": "t", "kind": "absolute", "fire_at": 1, "deleted_at": None},
        {"id": "r5", "task_id": "t", "kind": "deadline", "offset_minutes": 0, "deleted_at": None},
    ]
    respx.get(f"{OT}/api/v1/reminders").mock(
        return_value=httpx.Response(200, json={"reminders": rows})
    )
    assert {t["id"]: t["reminders"] for t in ot.tasks()} == {"t": [-30, 0], "u": []}

    respx.patch(f"{OT}/api/v1/tasks/t").mock(return_value=httpx.Response(200, json={"id": "t"}))
    gone = respx.delete(f"{OT}/api/v1/reminders/r2").mock(return_value=httpx.Response(204))
    new = respx.post(f"{OT}/api/v1/reminders").mock(
        return_value=httpx.Response(201, json={"id": "r9", "task_id": "t", "kind": "relative"})
    )
    resp = ot.update_task("t", {"title": "x", "reminders": [0, -120]})
    assert resp["reminders"] == [-120, 0]
    assert gone.called
    assert json.loads(new.calls[0].request.content) == {
        "task_id": "t",
        "kind": "relative",
        "offset_minutes": -120,
    }
    assert new.calls[0].request.headers["Idempotency-Key"].startswith("ticktick-opentodo-sync-")
    # Unchanged reminders: only the task is written.
    ot.update_task("t", {"title": "y", "reminders": [-120, 0]})
    assert new.call_count == 1 and gone.call_count == 1


@respx.mock
def test_opentodo_reminders_refuse_an_odd_listing() -> None:
    ot = OpenTodo(http(OT))
    respx.get(f"{OT}/api/v1/tasks").mock(return_value=httpx.Response(200, json={"tasks": []}))
    respx.get(f"{OT}/api/v1/reminders").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(TypeError):
        ot.tasks()


@respx.mock
def test_redirect_is_an_error_not_an_empty_answer() -> None:
    """2026-11-25: OpenTodo's host answered every call with an empty 308 to itself."""
    respx.get(f"{OT}/api/v1/projects").mock(
        return_value=httpx.Response(308, headers={"Location": f"{OT}/api/v1/projects"})
    )
    with pytest.raises(ApiError, match="308"):
        OpenTodo(http(OT)).projects()


@respx.mock
@pytest.mark.parametrize("body", [None, [], {"other": []}])
def test_odd_responses_never_read_as_nothing(body: object) -> None:
    content = b"" if body is None else json.dumps(body).encode()
    for path in ("/api/v1/me", "/api/v1/projects", "/api/v1/labels"):
        respx.get(f"{OT}{path}").mock(return_value=httpx.Response(200, content=content))
    respx.get(f"{TT}/open/v1/project").mock(return_value=httpx.Response(200, content=content))
    ot = OpenTodo(http(OT))
    if not isinstance(body, dict):  # a dict without "encryption" is a server without E2EE
        with pytest.raises(TypeError):
            ot.begin_pass()
    with pytest.raises(TypeError):
        ot.projects()
    with pytest.raises(TypeError):
        ot.labels()
    if not isinstance(body, list):  # a list is TickTick's normal shape
        with pytest.raises(TypeError):
            TickTick(http(TT)).projects()


@respx.mock
@pytest.mark.parametrize("body", [None, [], {}, {"errorCode": "x", "errorMessage": "y"}])
def test_odd_list_data_is_an_error_not_an_empty_list(body: object) -> None:
    """Read as "no tasks", every paired task in the list looked deleted (review #6)."""
    content = b"" if body is None else json.dumps(body).encode()
    respx.get(f"{TT}/open/v1/project/p/data").mock(
        return_value=httpx.Response(200, content=content)
    )
    with pytest.raises(TypeError, match="list p"):
        TickTick(http(TT)).project_data("p")


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"project": {"id": "p"}, "tasks": [], "columns": []},
        {"tasks": [{"id": "t"}]},
        {"project": {"id": "p"}, "columns": []},  # an empty list without "tasks"
    ],
)
def test_list_data(body: dict[str, object]) -> None:
    respx.get(f"{TT}/open/v1/project/p/data").mock(return_value=httpx.Response(200, json=body))
    assert TickTick(http(TT)).project_data("p") == body


@respx.mock
def test_get_task_answers() -> None:
    tt = TickTick(http(TT))
    route = respx.get(f"{TT}/open/v1/project/p/task/t")
    route.mock(return_value=httpx.Response(200, json={"id": "t", "status": 2}))
    assert tt.get_task("p", "t") == {"id": "t", "status": 2}
    for gone in (httpx.Response(404), httpx.Response(200), httpx.Response(200, json={})):
        route.mock(return_value=gone)
        assert tt.get_task("p", "t") is None
    odd_answers: list[object] = [[], {"errorCode": "x"}, {"id": "another"}]
    for odd in odd_answers:
        route.mock(return_value=httpx.Response(200, json=odd))
        with pytest.raises(TypeError, match="task t"):
            tt.get_task("p", "t")


@respx.mock
def test_habit_checkins_range_includes_the_last_day() -> None:
    """TickTick's ``to`` is exclusive (seen 2026-11-25: today's check-ins were missing)."""
    route = respx.get(f"{TT}/open/v1/habit/checkins").mock(
        return_value=httpx.Response(200, json=[])
    )
    TickTick(http(TT)).habit_checkins(["h1", "h2"], 20261126, 20261231)
    params = route.calls[0].request.url.params
    assert (params["habitIds"], params["from"], params["to"]) == ("h1,h2", "20261126", "20270101")
