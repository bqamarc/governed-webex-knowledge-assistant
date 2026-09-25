from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import Settings
from .db import database_ready
from .models import MESSAGE_ID_MAX_LENGTH
from .runtime import Runtime, build_runtime
from .security import delivery_id, verify_webhook_signature

MAX_WEBHOOK_BYTES = 1024 * 1024


async def read_bounded_body(request: Request) -> bytes:
    parts: list[bytes] = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        parts.append(part)
    return b"".join(parts)


def minimal_event(message_id: str) -> dict[str, object]:
    return {
        "resource": "messages",
        "event": "created",
        "data": {"id": message_id},
    }


def create_app(settings: Settings | None = None, *, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate_for_role("api")
    runtime = runtime or build_runtime(
        settings,
        create_schema=settings.environment != "production",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(
        title="Governed Webex Knowledge Assistant",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.get("/livez")
    def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        ready = database_ready(runtime.engine) and bool(runtime.manifest.records)
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if ready else "not-ready"},
        )

    @app.post("/webhooks/webex", status_code=status.HTTP_202_ACCEPTED)
    async def webex_webhook(request: Request) -> dict[str, object]:
        raw_body = await read_bounded_body(request)
        signature = request.headers.get("x-spark-signature")
        if not verify_webhook_signature(raw_body, signature, settings.webex_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Webhook payload must be an object")
        resource = payload.get("resource")
        event = payload.get("event")
        if not isinstance(resource, str) or not resource or not isinstance(event, str) or not event:
            raise HTTPException(status_code=400, detail="Webhook resource and event are required")
        if resource != "messages" or event != "created":
            return {"accepted": False, "status": "ignored"}
        data = payload.get("data")
        message_id = data.get("id") if isinstance(data, dict) else None
        if (
            not isinstance(message_id, str)
            or not message_id.strip()
            or len(message_id) > MESSAGE_ID_MAX_LENGTH
        ):
            raise HTTPException(status_code=400, detail="Webhook message id is invalid")
        accepted = runtime.queue.enqueue(delivery_id(raw_body), minimal_event(message_id))
        return {"accepted": accepted, "status": "queued" if accepted else "duplicate"}

    return app
