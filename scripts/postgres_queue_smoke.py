#!/usr/bin/env python3
"""Exercise durable queue leasing against the configured PostgreSQL database."""

from __future__ import annotations

import os
from uuid import uuid4

from sqlalchemy import delete

from webex_knowledge_assistant.db import build_engine, build_session_factory
from webex_knowledge_assistant.jobs import JobQueue
from webex_knowledge_assistant.models import WebhookJob


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("PostgreSQL queue smoke requires a PostgreSQL DATABASE_URL")

    engine = build_engine(database_url)
    factory = build_session_factory(engine)
    queue = JobQueue(factory, lease_seconds=10, max_attempts=2)
    prefix = f"ci-queue-{uuid4()}"
    job_ids = (f"{prefix}-a", f"{prefix}-b")
    try:
        for job_id in job_ids:
            assert queue.enqueue(job_id, {"event": "synthetic"})
        first = queue.lease_next()
        second = queue.lease_next()
        assert first is not None and second is not None
        assert {first.id, second.id} == set(job_ids)
        queue.complete(first.id)
        assert queue.fail(second.id, RuntimeError("synthetic"), retry_after_seconds=0) == "retry"
        retry = queue.lease_next()
        assert retry is not None and retry.id == second.id and retry.attempt == 2
        queue.complete(retry.id)
        with factory() as session:
            states = {
                job.id: job.status
                for job in session.query(WebhookJob).filter(WebhookJob.id.in_(job_ids)).all()
            }
        assert states == {job_id: "completed" for job_id in job_ids}
    finally:
        with factory() as session:
            session.execute(delete(WebhookJob).where(WebhookJob.id.in_(job_ids)))
            session.commit()
        engine.dispose()
    print("PostgreSQL queue smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
