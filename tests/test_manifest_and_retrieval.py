from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from webex_knowledge_assistant.manifest import KnowledgeManifest, ManifestError, load_manifest
from webex_knowledge_assistant.retrieval import Answer, Citation, RetrievalProvider, render_answer


def test_reviewed_synthetic_record_answers_with_title_citation(settings) -> None:
    manifest = load_manifest(settings.knowledge_manifest)
    answer = RetrievalProvider(manifest, min_score=0.3).answer(
        "Where should an access token be protected?"
    )
    assert answer.answered
    assert answer.reason == "reviewed-evidence"
    assert answer.citations[0].title == "Example Handbook"
    assert "**Source**" in render_answer(answer)


def test_unmatched_or_unanswerable_content_abstains(settings) -> None:
    manifest = load_manifest(settings.knowledge_manifest)
    provider = RetrievalProvider(manifest, min_score=0.3)
    assert not provider.answer("What is the unreviewed procedure?").answered
    assert provider.answer("What is the weather?").reason == "insufficient-reviewed-evidence"


def test_conflicting_equal_matches_abstain() -> None:
    manifest = KnowledgeManifest.model_validate(
        {
            "schema_version": 1,
            "source": {
                "id": "example",
                "title": "Example",
                "uri": "urn:example:source",
                "source_class": "synthetic",
                "audience": "public",
                "reviewed": True,
            },
            "records": [
                {
                    "id": "first",
                    "question": "How do I rotate a token?",
                    "keywords": ["rotate", "token"],
                    "answer": "Use procedure A.",
                    "answerable": True,
                    "citation_mode": "title_only",
                },
                {
                    "id": "second",
                    "question": "How do I rotate a token?",
                    "keywords": ["rotate", "token"],
                    "answer": "Use procedure B.",
                    "answerable": True,
                    "citation_mode": "title_only",
                },
            ],
        }
    )
    answer = RetrievalProvider(manifest, min_score=0.3).answer("How do I rotate a token?")
    assert not answer.answered
    assert answer.reason == "conflicting-reviewed-evidence"


def test_restricted_answerable_source_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "id": "restricted-example",
                    "title": "Restricted Example",
                    "uri": "urn:example:restricted",
                    "source_class": "restricted",
                    "audience": "restricted",
                    "reviewed": True,
                },
                "records": [
                    {
                        "id": "bad-record",
                        "question": "May this answer?",
                        "keywords": ["answer"],
                        "answer": "It must not answer.",
                        "answerable": True,
                        "citation_mode": "none",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="Only public or synthetic"):
        load_manifest(path)


def test_url_citation_requires_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        KnowledgeManifest.model_validate(
            {
                "schema_version": 1,
                "source": {
                    "id": "example",
                    "title": "Example",
                    "uri": "urn:example:source",
                    "source_class": "synthetic",
                    "audience": "public",
                    "reviewed": True,
                },
                "records": [
                    {
                        "id": "record",
                        "question": "Where is the guide?",
                        "keywords": ["guide"],
                        "answer": "Use the guide.",
                        "answerable": True,
                        "citation_mode": "url",
                        "source_url": "http://example.com/guide",
                    }
                ],
            }
        )


def test_answerable_record_requires_a_citation() -> None:
    with pytest.raises(ValueError, match="require a title or URL citation"):
        KnowledgeManifest.model_validate(
            {
                "schema_version": 1,
                "source": {
                    "id": "example",
                    "title": "Example",
                    "uri": "urn:example:source",
                    "source_class": "synthetic",
                    "audience": "public",
                    "reviewed": True,
                },
                "records": [
                    {
                        "id": "record",
                        "question": "Where is the guide?",
                        "keywords": ["guide"],
                        "answer": "Use the guide.",
                        "answerable": True,
                        "citation_mode": "none",
                    }
                ],
            }
        )


def test_multiword_keywords_are_tokenized() -> None:
    manifest = KnowledgeManifest.model_validate(
        {
            "schema_version": 1,
            "source": {
                "id": "example",
                "title": "Example",
                "uri": "urn:example:source",
                "source_class": "synthetic",
                "audience": "public",
                "reviewed": True,
            },
            "records": [
                {
                    "id": "record",
                    "question": "Where is information kept?",
                    "keywords": ["runtime secrets"],
                    "answer": "Use the managed store.",
                    "answerable": True,
                    "citation_mode": "title_only",
                }
            ],
        }
    )
    answer = RetrievalProvider(manifest, min_score=1.0).answer("runtime secrets")
    assert answer.answered


def test_rendering_escapes_untrusted_markdown_and_enforces_byte_budget() -> None:
    answer = Answer(
        answered=True,
        text=(
            "<spark-mention>person</spark-mention> **bold** "
            "[link](https://example.com) www.example.com ~~strike~~ &lt;tag&gt;\x00\n\n"
            "- safe line " + "🙂" * 100
        ),
        reason="reviewed-evidence",
        citations=(Citation(title="[Example] <b>source</b>", url="https://example.com/guide"),),
    )
    rendered = render_answer(answer, max_bytes=400)
    assert "<spark-mention>" not in rendered
    assert "**bold**" not in rendered
    assert "[link](https://example.com)" not in rendered
    assert rendered.count("https://example.com") == 1
    assert "www.example.com" not in rendered
    assert "~~strike~~" not in rendered
    assert "external link omitted" in rendered
    assert "&lt;tag&gt;" not in rendered
    assert "\x00" not in rendered
    assert "https://example.com/guide" in rendered
    assert "<" not in rendered and ">" not in rendered
    assert len(rendered.encode("utf-8")) <= 400
    rendered.encode("utf-8").decode("utf-8")
    assert "\n\n" in rendered


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://example.com/guide",
        "javascript:alert(1)",
        "https://example.com/guide) [unsafe](https://example.com",
    ],
)
def test_rendering_never_emits_an_unvalidated_citation_link(unsafe_url: str) -> None:
    answer = Answer(
        answered=True,
        text="Safe answer.",
        reason="reviewed-evidence",
        citations=(Citation(title="Example", url=unsafe_url),),
    )
    rendered = render_answer(answer)
    assert unsafe_url not in rendered
    assert rendered.endswith("- Example")


@pytest.mark.parametrize(
    ("effective_at", "expires_at"),
    [
        ("2031-01-01T00:00:00Z", None),
        ("2020-01-01T00:00:00Z", "2029-01-01T00:00:00Z"),
    ],
)
def test_future_or_expired_source_abstains(effective_at, expires_at) -> None:
    manifest = KnowledgeManifest.model_validate(
        {
            "schema_version": 1,
            "source": {
                "id": "time-bounded-example",
                "title": "Time-bounded Example",
                "uri": "urn:example:time-bounded",
                "source_class": "synthetic",
                "audience": "public",
                "reviewed": True,
                "effective_at": effective_at,
                "expires_at": expires_at,
            },
            "records": [
                {
                    "id": "record",
                    "question": "Where is the guide?",
                    "keywords": ["guide"],
                    "answer": "Use the guide.",
                    "answerable": True,
                    "citation_mode": "title_only",
                }
            ],
        }
    )
    answer = RetrievalProvider(manifest).answer(
        "Where is the guide?", now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert not answer.answered
    assert answer.reason == "source-not-current"


def test_source_effective_window_requires_order() -> None:
    with pytest.raises(ValueError, match="later than"):
        KnowledgeManifest.model_validate(
            {
                "schema_version": 1,
                "source": {
                    "id": "time-bounded-example",
                    "title": "Time-bounded Example",
                    "uri": "urn:example:time-bounded",
                    "source_class": "synthetic",
                    "audience": "public",
                    "reviewed": True,
                    "effective_at": "2030-01-02T00:00:00Z",
                    "expires_at": "2030-01-01T00:00:00Z",
                },
                "records": [
                    {
                        "id": "record",
                        "question": "Where is the guide?",
                        "keywords": ["guide"],
                        "answer": "Not yet.",
                        "answerable": False,
                        "citation_mode": "none",
                    }
                ],
            }
        )
