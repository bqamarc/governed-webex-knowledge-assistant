from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import WebhookJob

MAX_RETRY_DELAY_SECONDS = 300
SAFE_ERROR_CODE_RE = re.compile(r"^[a-z0-9_:-]+$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class JobLease:
    id: str
    payload: dict
    attempt: int


class JobQueue:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int,
        max_attempts: int,
    ) -> None:
        self.session_factory = session_factory
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def enqueue(self, job_id: str, payload: dict) -> bool:
        now = _utc_now()
        with self.session_factory() as session:
            session.add(
                WebhookJob(
                    id=job_id,
                    payload=payload,
                    status="queued",
                    max_attempts=self.max_attempts,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
        return True

    def lease_next(self) -> JobLease | None:
        now = _utc_now()
        with self.session_factory() as session:
            session.execute(
                update(WebhookJob)
                .where(
                    WebhookJob.status == "leased",
                    WebhookJob.lease_until <= now,
                    WebhookJob.attempts >= WebhookJob.max_attempts,
                )
                .values(
                    status="dead_letter",
                    lease_until=None,
                    last_error="Lease expired after maximum attempts",
                    updated_at=now,
                )
            )
            session.commit()
            statement = (
                select(WebhookJob)
                .where(
                    or_(
                        and_(
                            WebhookJob.status.in_(("queued", "retry")),
                            WebhookJob.available_at <= now,
                        ),
                        and_(
                            WebhookJob.status == "leased",
                            WebhookJob.lease_until <= now,
                            WebhookJob.attempts < WebhookJob.max_attempts,
                        ),
                    )
                )
                .order_by(WebhookJob.created_at, WebhookJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = session.scalars(statement).first()
            if job is None:
                return None
            job.status = "leased"
            job.attempts += 1
            job.lease_until = now + timedelta(seconds=self.lease_seconds)
            job.updated_at = now
            session.commit()
            return JobLease(id=job.id, payload=dict(job.payload), attempt=job.attempts)

    def complete(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = session.get(WebhookJob, job_id)
            if job is None:
                return
            job.status = "completed"
            job.lease_until = None
            job.last_error = None
            job.updated_at = _utc_now()
            session.commit()

    def fail(
        self,
        job_id: str,
        error: Exception,
        *,
        retry_after_seconds: int | None = None,
        terminal: bool = False,
    ) -> str:
        now = _utc_now()
        with self.session_factory() as session:
            job = session.get(WebhookJob, job_id)
            if job is None:
                return "missing"
            error_code = getattr(error, "code", "")
            if not isinstance(error_code, str) or not SAFE_ERROR_CODE_RE.fullmatch(error_code):
                error_code = f"processing_error:{type(error).__name__}"
            job.last_error = error_code[:500]
            job.lease_until = None
            job.updated_at = now
            if terminal or job.attempts >= job.max_attempts:
                job.status = "dead_letter"
            else:
                job.status = "retry"
                delay = (
                    min(max(retry_after_seconds, 0), MAX_RETRY_DELAY_SECONDS)
                    if retry_after_seconds is not None
                    else min(2**job.attempts, 60)
                )
                job.available_at = now + timedelta(seconds=delay)
            session.commit()
            return job.status
