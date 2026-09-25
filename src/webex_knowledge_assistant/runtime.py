from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from .bot import BotService
from .config import Settings
from .db import build_engine, build_session_factory, initialize_schema
from .jobs import JobQueue
from .manifest import KnowledgeManifest, load_manifest
from .retrieval import RetrievalProvider
from .webex import WebexClient


@dataclass
class Runtime:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    manifest: KnowledgeManifest
    retrieval: RetrievalProvider
    queue: JobQueue
    webex_client: WebexClient | None = None

    def bot_service(self) -> BotService:
        if self.webex_client is None:
            self.webex_client = WebexClient(
                self.settings.webex_bot_token,
                base_url=self.settings.webex_api_base_url,
            )
        return BotService(
            settings=self.settings,
            session_factory=self.session_factory,
            webex_client=self.webex_client,
            retrieval=self.retrieval,
        )

    def close(self) -> None:
        if self.webex_client is not None:
            self.webex_client.close()
        self.engine.dispose()


def build_runtime(
    settings: Settings,
    *,
    create_schema: bool = False,
    webex_client: WebexClient | None = None,
) -> Runtime:
    engine = build_engine(settings.database_url)
    if create_schema:
        initialize_schema(engine)
    factory = build_session_factory(engine)
    manifest = load_manifest(settings.knowledge_manifest)
    retrieval = RetrievalProvider(manifest, min_score=settings.retrieval_min_score)
    queue = JobQueue(
        factory,
        lease_seconds=settings.job_lease_seconds,
        max_attempts=settings.job_max_attempts,
    )
    return Runtime(
        settings=settings,
        engine=engine,
        session_factory=factory,
        manifest=manifest,
        retrieval=retrieval,
        queue=queue,
        webex_client=webex_client,
    )
