from __future__ import annotations

from datetime import UTC, datetime, timedelta

from webex_knowledge_assistant.jobs import JobQueue
from webex_knowledge_assistant.models import WebhookJob


def test_queue_deduplicates_leases_and_completes(runtime) -> None:
    queue = runtime.queue
    assert queue.enqueue("job-1", {"event": "created"})
    assert not queue.enqueue("job-1", {"event": "created"})
    lease = queue.lease_next()
    assert lease is not None
    assert lease.id == "job-1"
    assert lease.attempt == 1
    queue.complete("job-1")
    assert queue.lease_next() is None
    with runtime.session_factory() as session:
        assert session.get(WebhookJob, "job-1").status == "completed"


def test_queue_retries_then_dead_letters(runtime) -> None:
    queue: JobQueue = runtime.queue
    assert queue.enqueue("job-2", {"event": "created"})
    first = queue.lease_next()
    assert first is not None
    assert queue.fail(first.id, RuntimeError("synthetic failure")) == "retry"
    with runtime.session_factory() as session:
        job = session.get(WebhookJob, "job-2")
        job.available_at = job.created_at
        session.commit()
    second = queue.lease_next()
    assert second is not None
    assert queue.fail(second.id, RuntimeError("synthetic failure")) == "dead_letter"
    with runtime.session_factory() as session:
        assert session.get(WebhookJob, "job-2").status == "dead_letter"


def test_missing_queue_updates_are_noops(runtime) -> None:
    runtime.queue.complete("missing")
    assert runtime.queue.fail("missing", ValueError("missing")) == "missing"


def test_expired_lease_is_recovered_then_dead_lettered(runtime) -> None:
    queue = runtime.queue
    assert queue.enqueue("expired-job", {"event": "created"})
    first = queue.lease_next()
    assert first is not None and first.attempt == 1

    with runtime.session_factory() as session:
        job = session.get(WebhookJob, "expired-job")
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    second = queue.lease_next()
    assert second is not None and second.id == "expired-job" and second.attempt == 2
    with runtime.session_factory() as session:
        job = session.get(WebhookJob, "expired-job")
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    assert queue.lease_next() is None
    with runtime.session_factory() as session:
        job = session.get(WebhookJob, "expired-job")
        assert job.status == "dead_letter"
        assert job.lease_until is None
        assert job.last_error == "Lease expired after maximum attempts"


def test_failure_state_does_not_persist_exception_content(runtime) -> None:
    assert runtime.queue.enqueue("private-error", {"event": "created"})
    lease = runtime.queue.lease_next()
    assert lease is not None
    secret = "do-not-persist-this-secret"  # pragma: allowlist secret
    assert runtime.queue.fail(lease.id, RuntimeError(secret)) == "retry"
    with runtime.session_factory() as session:
        job = session.get(WebhookJob, "private-error")
        assert job.last_error == "processing_error:RuntimeError"
        assert secret not in job.last_error


def test_terminal_failure_dead_letters_immediately(runtime) -> None:
    assert runtime.queue.enqueue("terminal", {"event": "created"})
    lease = runtime.queue.lease_next()
    assert lease is not None
    assert runtime.queue.fail(lease.id, ValueError("synthetic"), terminal=True) == "dead_letter"
