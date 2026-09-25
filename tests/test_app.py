from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from webex_knowledge_assistant.app import create_app
from webex_knowledge_assistant.models import WebhookJob
from webex_knowledge_assistant.runtime import build_runtime


def signed_headers(body: bytes, secret: str = "synthetic-webhook-secret") -> dict[str, str]:
    signature = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return {"content-type": "application/json", "x-spark-signature": signature}


def test_health_and_signed_enqueue(runtime, settings) -> None:
    app = create_app(settings, runtime=runtime)
    body = json.dumps(
        {
            "resource": "messages",
            "event": "created",
            "data": {
                "id": "message-1",
                "roomId": "untrusted-room",
                "roomType": "group",
                "personId": "untrusted-person",
                "unexpected": "must-not-persist",
            },
            "unexpected": "must-not-persist",
        },
        separators=(",", ":"),
    ).encode()
    with TestClient(app) as client:
        assert client.get("/livez").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 200
        first = client.post("/webhooks/webex", content=body, headers=signed_headers(body))
        duplicate = client.post("/webhooks/webex", content=body, headers=signed_headers(body))
    assert first.status_code == 202
    assert first.json() == {"accepted": True, "status": "queued"}
    assert duplicate.json() == {"accepted": False, "status": "duplicate"}
    with runtime.session_factory() as session:
        job = session.scalars(select(WebhookJob)).one()
        assert job.payload == {
            "resource": "messages",
            "event": "created",
            "data": {"id": "message-1"},
        }


def test_webhook_rejects_bad_signature_json_and_oversize(runtime, settings) -> None:
    app = create_app(settings, runtime=runtime)
    with TestClient(app) as client:
        assert client.post("/webhooks/webex", content=b"{}", headers={}).status_code == 401
        malformed = b"{"
        assert (
            client.post(
                "/webhooks/webex", content=malformed, headers=signed_headers(malformed)
            ).status_code
            == 400
        )
        scalar = b"[]"
        response = client.post(
            "/webhooks/webex",
            content=scalar,
            headers=signed_headers(scalar),
        )
        assert response.status_code == 400
        large = b" " * (1024 * 1024 + 1)
        assert (
            client.post("/webhooks/webex", content=large, headers=signed_headers(large)).status_code
            == 413
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"resource": "messages"},
        {"resource": "messages", "event": "created"},
        {"resource": "messages", "event": "created", "data": {}},
        {"resource": "messages", "event": "created", "data": {"id": ""}},
        {"resource": "messages", "event": "created", "data": {"id": " "}},
        {"resource": "messages", "event": "created", "data": {"id": 123}},
        {"resource": "messages", "event": "created", "data": {"id": "x" * 161}},
    ],
)
def test_malformed_event_is_rejected_without_queueing(runtime, settings, payload) -> None:
    app = create_app(settings, runtime=runtime)
    body = json.dumps(payload, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post("/webhooks/webex", content=body, headers=signed_headers(body))
    assert response.status_code == 400
    with runtime.session_factory() as session:
        assert session.scalars(select(WebhookJob)).all() == []


@pytest.mark.parametrize(
    "payload",
    [
        {"resource": "memberships", "event": "created"},
        {"resource": "messages", "event": "deleted", "data": {"id": "message-1"}},
    ],
)
def test_out_of_scope_event_is_acknowledged_without_queueing(runtime, settings, payload) -> None:
    app = create_app(settings, runtime=runtime)
    body = json.dumps(payload, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post("/webhooks/webex", content=body, headers=signed_headers(body))
    assert response.status_code == 202
    assert response.json() == {"accepted": False, "status": "ignored"}
    with runtime.session_factory() as session:
        assert session.scalars(select(WebhookJob)).all() == []


def test_readiness_requires_migrated_schema(settings, tmp_path: Path) -> None:
    uninitialized = settings.model_copy(
        update={"database_url": f"sqlite:///{tmp_path / 'uninitialized.db'}"}
    )
    runtime = build_runtime(uninitialized, create_schema=False)
    app = create_app(uninitialized, runtime=runtime)
    with TestClient(app) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/readyz").status_code == 503
