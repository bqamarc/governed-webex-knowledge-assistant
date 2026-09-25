from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .manifest import KnowledgeManifest, KnowledgeRecord

TOKEN_RE = re.compile(r"[a-z0-9]+")
MARKDOWN_META_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>&~])")
ANSWER_URL_RE = re.compile(
    r"(?i)(?<![@\w])(?:"
    r"(?:https?://|www\.)[^\s<>()\[\]]+|"
    r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>()\[\]]*)?"
    r")"
)
MAX_OUTBOUND_BYTES = 7000
STOPWORDS = frozenset(
    {"a", "an", "and", "are", "do", "for", "how", "i", "in", "is", "of", "the", "to", "what"}
)


def tokens(text: str) -> frozenset[str]:
    return frozenset(token for token in TOKEN_RE.findall(text.casefold()) if token not in STOPWORDS)


def similarity(question: str, record: KnowledgeRecord) -> float:
    query = tokens(question)
    expected = tokens(record.question).union(*(tokens(keyword) for keyword in record.keywords))
    if not query or not expected:
        return 0.0
    return len(query.intersection(expected)) / max(len(query), 1)


@dataclass(frozen=True)
class Citation:
    title: str
    url: str | None = None


@dataclass(frozen=True)
class Answer:
    answered: bool
    text: str
    reason: str
    citations: tuple[Citation, ...] = ()
    score: float = 0.0


class RetrievalProvider:
    def __init__(self, manifest: KnowledgeManifest, *, min_score: float = 0.34) -> None:
        self.manifest = manifest
        self.min_score = min_score

    def answer(self, question: str, *, now: datetime | None = None) -> Answer:
        current_time = now or datetime.now(UTC)
        if current_time.utcoffset() is None:
            raise ValueError("now must include a timezone")
        source = self.manifest.source
        if any(record.answerable for record in self.manifest.records) and (
            (source.effective_at is not None and current_time < source.effective_at)
            or (source.expires_at is not None and current_time >= source.expires_at)
        ):
            return Answer(
                answered=False,
                text=(
                    "The reviewed source is not currently effective. "
                    "Please ask a reviewer to publish a current source."
                ),
                reason="source-not-current",
            )
        ranked = sorted(
            (
                (similarity(question, record), record)
                for record in self.manifest.records
                if record.answerable
            ),
            key=lambda item: (-item[0], item[1].id),
        )
        if not ranked or ranked[0][0] < self.min_score:
            return Answer(
                answered=False,
                text=(
                    "I do not have enough reviewed evidence to answer that question. "
                    "Please add or review an eligible knowledge record."
                ),
                reason="insufficient-reviewed-evidence",
                score=ranked[0][0] if ranked else 0.0,
            )

        score, record = ranked[0]
        if len(ranked) > 1 and abs(score - ranked[1][0]) < 0.05:
            other = ranked[1][1]
            if record.answer.strip() != other.answer.strip():
                return Answer(
                    answered=False,
                    text=(
                        "The reviewed evidence is ambiguous for that question. "
                        "Please clarify the subject or have a reviewer resolve the conflict."
                    ),
                    reason="conflicting-reviewed-evidence",
                    score=score,
                )

        citation = Citation(
            title=self.manifest.source.title,
            url=record.source_url if record.citation_mode == "url" else None,
        )
        citations = () if record.citation_mode == "none" else (citation,)
        return Answer(
            answered=True,
            text=record.answer,
            reason="reviewed-evidence",
            citations=citations,
            score=score,
        )


def _safe_plain_markdown(value: str) -> str:
    without_urls = ANSWER_URL_RE.sub("[external link omitted]", value)
    without_controls = "".join(
        character
        if character == "\n" or not unicodedata.category(character).startswith("C")
        else " "
        for character in without_urls.replace("\r\n", "\n").replace("\r", "\n")
    )
    lines = [" ".join(line.split()) for line in without_controls.split("\n")]
    normalized = "\n".join(lines).strip()
    while "\n\n\n" in normalized:
        normalized = normalized.replace("\n\n\n", "\n\n")
    return MARKDOWN_META_RE.sub(r"\\\1", html.escape(normalized, quote=True))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = "... [truncated]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore").rstrip()
    return prefix + marker


def _safe_citation_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            or character in "()[]<>\\`\"'"
            for character in value
        )
    ):
        return None
    return value


def render_answer(answer: Answer, *, max_bytes: int = MAX_OUTBOUND_BYTES) -> str:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    body = _safe_plain_markdown(answer.text)
    if not answer.citations:
        return _truncate_utf8(body, max_bytes)
    lines = ["", "**Source**"]
    for citation in answer.citations:
        label = _safe_plain_markdown(citation.title)
        citation_url = _safe_citation_url(citation.url)
        lines.append(f"- [{label}]({citation_url})" if citation_url else f"- {label}")
    citation_block = "\n".join(lines)
    citation_bytes = len(citation_block.encode("utf-8"))
    if citation_bytes >= max_bytes:
        return _truncate_utf8(citation_block.lstrip(), max_bytes)
    body = _truncate_utf8(body, max_bytes - citation_bytes)
    return body + citation_block
