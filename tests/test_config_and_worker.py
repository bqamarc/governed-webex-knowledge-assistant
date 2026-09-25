from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from webex_knowledge_assistant.config import ConfigurationError
from webex_knowledge_assistant.models import ProcessedMessage, WebhookJob
from webex_knowledge_assistant.webex import (
    AmbiguousWebexAPIError,
    PermanentWebexAPIError,
    RetryableWebexAPIError,
)
from webex_knowledge_assistant.worker import Worker


def test_production_configuration_fails_closed(settings) -> None:
    production = settings.model_copy(update={"environment": "production"})
    with pytest.raises(ConfigurationError, match="shared durable database"):
        production.validate_for_role("api")


def test_production_pins_webex_api_and_direct_message_audience(settings) -> None:
    production = settings.model_copy(
        update={
            "environment": "production",
            "database_url": "postgresql://u:p@localhost/d",  # pragma: allowlist secret
            "webex_api_base_url": "https://example.com/v1",
        }
    )
    production.validate_for_role("api")
    with pytest.raises(ConfigurationError, match="webexapis.com"):
        production.validate_for_role("worker")

    direct = production.model_copy(
        update={
            "webex_api_base_url": "https://webexapis.com/v1",
            "allowed_webex_space_ids": "",
            "allow_direct_messages": True,
            "allowed_webex_person_ids": "",
        }
    )
    with pytest.raises(ConfigurationError, match="exact ALLOWED_WEBEX_PERSON_IDS"):
        direct.validate_for_role("worker")
    direct.model_copy(update={"allowed_webex_person_ids": "person-1"}).validate_for_role("worker")


def test_production_roles_receive_only_their_required_secrets(settings) -> None:
    common = {
        "environment": "production",
        "database_url": "postgresql://u:p@localhost/d",  # pragma: allowlist secret
    }
    migration = settings.model_copy(
        update={
            **common,
            "webex_bot_token": "",
            "webex_bot_person_id": "",
            "webex_webhook_secret": "",
        }
    )
    migration.validate_for_role("migration")

    api = migration.model_copy(update={"webex_webhook_secret": "synthetic-webhook-secret"})
    api.validate_for_role("api")
    with pytest.raises(ConfigurationError, match="WEBEX_WEBHOOK_SECRET"):
        migration.validate_for_role("api")

    worker = settings.model_copy(update={**common, "webex_webhook_secret": ""})
    worker.validate_for_role("worker")


def test_worker_processes_a_queued_message(runtime) -> None:
    runtime.fake_webex.messages["worker-message"] = {
        "id": "worker-message",
        "roomId": "allowed-space",
        "roomType": "group",
        "personId": "person-1",
        "mentionedPeople": ["bot-person"],
        "markdown": "How should I protect an access token?",
    }
    assert runtime.queue.enqueue(
        "worker-job",
        {
            "resource": "messages",
            "event": "created",
            "data": {"id": "worker-message", "roomId": "allowed-space"},
        },
    )
    worker = Worker(runtime)
    assert worker.run_once()
    assert not worker.run_once()
    with runtime.session_factory() as session:
        assert session.get(WebhookJob, "worker-job").status == "completed"
    assert len(runtime.fake_webex.posts) == 1


def _queue_answerable_message(runtime, *, job_id: str, message_id: str) -> None:
    runtime.fake_webex.messages[message_id] = {
        "id": message_id,
        "roomId": "allowed-space",
        "roomType": "group",
        "personId": "person-1",
        "mentionedPeople": ["bot-person"],
        "markdown": "How should I protect an access token?",
    }
    assert runtime.queue.enqueue(
        job_id,
        {"resource": "messages", "event": "created", "data": {"id": message_id}},
    )


def test_worker_retries_known_non_acceptance_without_suppressing_reply(runtime) -> None:
    _queue_answerable_message(runtime, job_id="rate-job", message_id="rate-message")
    runtime.fake_webex.post_error = RetryableWebexAPIError(
        "Webex returned HTTP 429",
        code="webex_http_429",
        retry_after_seconds=120,
    )
    worker = Worker(runtime)
    before = datetime.now(UTC)
    assert worker.run_once()
    with runtime.session_factory() as session:
        job = session.get(WebhookJob, "rate-job")
        assert job.status == "retry"
        assert job.last_error == "webex_http_429"
        available_at = job.available_at.replace(tzinfo=UTC)
        assert available_at >= before + timedelta(seconds=119)
        assert session.get(ProcessedMessage, "rate-message") is None
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    runtime.fake_webex.post_error = None
    assert worker.run_once()
    with runtime.session_factory() as session:
        assert session.get(WebhookJob, "rate-job").status == "completed"
        assert session.get(ProcessedMessage, "rate-message").status == "completed"
    assert len(runtime.fake_webex.posts) == 1


def test_worker_dead_letters_permanent_4xx_and_releases_claim(runtime) -> None:
    _queue_answerable_message(runtime, job_id="bad-job", message_id="bad-message")
    runtime.fake_webex.post_error = PermanentWebexAPIError(
        "Webex returned HTTP 400",
        code="webex_http_400",
    )
    assert Worker(runtime).run_once()
    with runtime.session_factory() as session:
        job = session.get(WebhookJob, "bad-job")
        assert job.status == "dead_letter"
        assert job.last_error == "webex_http_400"
        assert session.get(ProcessedMessage, "bad-message") is None


def test_worker_does_not_replay_ambiguous_post_5xx(runtime) -> None:
    _queue_answerable_message(runtime, job_id="uncertain-job", message_id="uncertain-message")
    runtime.fake_webex.post_error = AmbiguousWebexAPIError(
        "Webex write outcome after HTTP 503 is unknown",
        code="webex_ambiguous_http_503",
    )
    worker = Worker(runtime)
    assert worker.run_once()
    with runtime.session_factory() as session:
        assert session.get(WebhookJob, "uncertain-job").status == "completed"
        assert session.get(ProcessedMessage, "uncertain-message").status == "ambiguous"

    runtime.fake_webex.post_error = None
    assert runtime.queue.enqueue(
        "duplicate-uncertain-job",
        {
            "resource": "messages",
            "event": "created",
            "data": {"id": "uncertain-message"},
        },
    )
    assert worker.run_once()
    with runtime.session_factory() as session:
        assert session.get(WebhookJob, "duplicate-uncertain-job").status == "completed"
    assert runtime.fake_webex.posts == []
