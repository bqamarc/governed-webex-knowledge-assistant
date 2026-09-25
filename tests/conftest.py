from __future__ import annotations

import json
from pathlib import Path

import pytest

from webex_knowledge_assistant.config import Settings
from webex_knowledge_assistant.runtime import build_runtime


class FakeWebexClient:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.post_error: Exception | None = None

    def get_message(self, message_id: str) -> dict:
        return dict(self.messages[message_id])

    def post_message(self, *, room_id: str, markdown: str, parent_id: str | None = None) -> dict:
        if self.post_error is not None:
            raise self.post_error
        post = {"roomId": room_id, "markdown": markdown, "parentId": parent_id}
        self.posts.append(post)
        return {"id": f"reply-{len(self.posts)}", **post}

    def close(self) -> None:
        return None


def write_manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "id": "example-handbook",
                    "title": "Example Handbook",
                    "uri": "urn:example:handbook",
                    "source_class": "synthetic",
                    "audience": "public",
                    "reviewed": True,
                    "effective_at": "2020-01-01T00:00:00Z",
                },
                "records": [
                    {
                        "id": "protect-token",
                        "question": "How should I protect an access token?",
                        "keywords": ["protect", "access", "token", "credential"],
                        "answer": "Keep the token in a managed runtime secret.",
                        "answerable": True,
                        "citation_mode": "title_only",
                    },
                    {
                        "id": "candidate-only",
                        "question": "What is the unreviewed procedure?",
                        "keywords": ["unreviewed", "procedure"],
                        "answer": "This record is not eligible.",
                        "answerable": False,
                        "citation_mode": "none",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    manifest = write_manifest(tmp_path / "manifest.json")
    return Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'assistant.db'}",
        knowledge_manifest=manifest,
        allowed_webex_space_ids="allowed-space",
        webex_bot_person_id="bot-person",
        webex_bot_token="synthetic-token",
        webex_webhook_secret="synthetic-webhook-secret",  # pragma: allowlist secret
        retrieval_min_score=0.3,
        job_lease_seconds=10,
        job_max_attempts=2,
    )


@pytest.fixture
def runtime(settings: Settings):
    fake = FakeWebexClient()
    value = build_runtime(settings, create_schema=True, webex_client=fake)
    value.fake_webex = fake
    yield value
    value.close()
