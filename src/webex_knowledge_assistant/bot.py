from __future__ import annotations

import html
import re
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import ProcessedMessage
from .redaction import redact_text
from .retrieval import RetrievalProvider, render_answer
from .webex import (
    AmbiguousWebexAPIError,
    PermanentWebexAPIError,
    RetryableWebexAPIError,
    WebexClient,
)

MENTION_RE = re.compile(r"<spark-mention\b[^>]*>.*?</spark-mention>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def clean_question(markdown: str) -> str:
    without_mentions = MENTION_RE.sub(" ", markdown)
    without_tags = TAG_RE.sub(" ", without_mentions)
    return " ".join(html.unescape(without_tags).split())


def message_mentions_bot(message: dict, bot_person_id: str) -> bool:
    mentioned = message.get("mentionedPeople") or []
    if bot_person_id in {str(item) for item in mentioned}:
        return True
    markdown = str(message.get("markdown") or "")
    escaped = re.escape(bot_person_id)
    return bool(re.search(rf"data-object-id=['\"]{escaped}['\"]", markdown, re.IGNORECASE))


@dataclass(frozen=True)
class ProcessingResult:
    status: str
    answered: bool = False


class BotService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        webex_client: WebexClient,
        retrieval: RetrievalProvider,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.webex_client = webex_client
        self.retrieval = retrieval

    def process(self, payload: dict) -> ProcessingResult:
        if payload.get("resource") != "messages" or payload.get("event") != "created":
            return ProcessingResult("ignored-event")
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return ProcessingResult("ignored-invalid")
        message_id = str(data.get("id") or "")
        if not message_id:
            return ProcessingResult("ignored-invalid")

        message = self.webex_client.get_message(message_id)
        fetched_message_id = str(message.get("id") or "")
        room_id = str(message.get("roomId") or "")
        room_type = str(message.get("roomType") or "")
        sender_id = str(message.get("personId") or "")
        if (
            fetched_message_id != message_id
            or not room_id
            or not sender_id
            or room_type not in {"direct", "group"}
        ):
            return ProcessingResult("ignored-invalid")
        if room_type == "direct":
            if not self.settings.allow_direct_messages:
                return ProcessingResult("ignored-direct-message")
            if sender_id not in self.settings.allowed_person_ids:
                return ProcessingResult("ignored-person")
        elif room_id not in self.settings.allowed_space_ids:
            return ProcessingResult("ignored-space")

        if sender_id and sender_id == self.settings.webex_bot_person_id:
            return ProcessingResult("ignored-self")
        if (
            room_type == "group"
            and self.settings.require_mention
            and not message_mentions_bot(message, self.settings.webex_bot_person_id)
        ):
            return ProcessingResult("ignored-not-mentioned")

        question = clean_question(str(message.get("markdown") or message.get("text") or ""))
        safe_question = redact_text(question).text.strip()
        if not safe_question:
            return ProcessingResult("ignored-empty")

        answer = self.retrieval.answer(safe_question)
        markdown = render_answer(answer)
        parent_id = str(message.get("parentId") or message_id) if room_type == "group" else None

        if not self._begin_post(message_id):
            return ProcessingResult("duplicate-message")
        try:
            self.webex_client.post_message(
                room_id=room_id,
                markdown=markdown,
                parent_id=parent_id,
            )
        except (RetryableWebexAPIError, PermanentWebexAPIError):
            self._release_post(message_id)
            raise
        except AmbiguousWebexAPIError:
            self._finish_post(message_id, "ambiguous")
            return ProcessingResult("ambiguous-post")
        except Exception:
            self._finish_post(message_id, "ambiguous")
            return ProcessingResult("ambiguous-post")
        self._finish_post(message_id, "completed")
        return ProcessingResult("completed", answered=answer.answered)

    def _begin_post(self, message_id: str) -> bool:
        with self.session_factory() as session:
            session.add(ProcessedMessage(message_id=message_id, status="posting"))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
        return True

    def _finish_post(self, message_id: str, status: str) -> None:
        with self.session_factory() as session:
            claim = session.get(ProcessedMessage, message_id)
            if claim is not None:
                claim.status = status
                session.commit()

    def _release_post(self, message_id: str) -> None:
        with self.session_factory() as session:
            claim = session.get(ProcessedMessage, message_id)
            if claim is not None:
                session.delete(claim)
                session.commit()
