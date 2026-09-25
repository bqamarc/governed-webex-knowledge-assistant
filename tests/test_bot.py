from __future__ import annotations

import pytest

from webex_knowledge_assistant.bot import BotService, clean_question, message_mentions_bot
from webex_knowledge_assistant.models import ProcessedMessage
from webex_knowledge_assistant.webex import AmbiguousWebexAPIError


def payload(message_id: str, *, room_id: str = "allowed-space") -> dict:
    return {
        "resource": "messages",
        "event": "created",
        "data": {"id": message_id, "roomId": room_id, "personId": "person-1"},
    }


def group_message(message_id: str, **overrides) -> dict:
    value = {
        "id": message_id,
        "roomId": "allowed-space",
        "roomType": "group",
        "personId": "person-1",
        "mentionedPeople": ["bot-person"],
        "markdown": (
            '<spark-mention data-object-id="bot-person">Assistant</spark-mention> '
            "How should I protect an access token?"
        ),
    }
    value.update(overrides)
    return value


def service(runtime) -> BotService:
    return runtime.bot_service()


def test_clean_question_removes_mention_and_markup() -> None:
    value = '<spark-mention data-object-id="bot-person">Assistant</spark-mention> <b>Hello</b>'
    assert clean_question(value) == "Hello"
    assert message_mentions_bot({"markdown": value}, "bot-person")


def test_allowed_mention_posts_one_grounded_answer(runtime) -> None:
    runtime.fake_webex.messages["message-1"] = group_message("message-1")
    result = service(runtime).process(payload("message-1"))
    assert result.status == "completed"
    assert result.answered
    assert len(runtime.fake_webex.posts) == 1
    assert "managed runtime secret" in runtime.fake_webex.posts[0]["markdown"]
    assert "Example Handbook" in runtime.fake_webex.posts[0]["markdown"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (group_message("space", roomId="different-space"), "ignored-space"),
        (group_message("self", personId="bot-person"), "ignored-self"),
        (
            group_message("mention", mentionedPeople=[], markdown="Question"),
            "ignored-not-mentioned",
        ),
        (
            group_message("event", roomType="group"),
            "ignored-event",
        ),
        (group_message("room-type", roomType="unknown"), "ignored-invalid"),
    ],
)
def test_policy_ignores_disallowed_messages(runtime, message, expected) -> None:
    message_id = message["id"]
    runtime.fake_webex.messages[message_id] = message
    value = payload(message_id)
    if expected == "ignored-event":
        value["event"] = "deleted"
    result = service(runtime).process(value)
    assert result.status == expected
    assert runtime.fake_webex.posts == []


def test_direct_messages_are_disabled(runtime) -> None:
    runtime.fake_webex.messages["direct"] = group_message("direct", roomType="direct")
    assert service(runtime).process(payload("direct")).status == "ignored-direct-message"


def test_direct_messages_require_an_exact_sender_allowlist(runtime) -> None:
    runtime.settings.allow_direct_messages = True
    runtime.settings.allowed_webex_person_ids = "allowed-person"
    runtime.fake_webex.messages["blocked-direct"] = group_message(
        "blocked-direct", roomType="direct", personId="different-person"
    )
    assert service(runtime).process(payload("blocked-direct")).status == "ignored-person"

    runtime.fake_webex.messages["allowed-direct"] = group_message(
        "allowed-direct", roomType="direct", personId="allowed-person"
    )
    assert service(runtime).process(payload("allowed-direct")).status == "completed"


def test_duplicate_message_does_not_post_twice(runtime) -> None:
    runtime.fake_webex.messages["duplicate"] = group_message("duplicate")
    assert service(runtime).process(payload("duplicate")).status == "completed"
    assert service(runtime).process(payload("duplicate")).status == "duplicate-message"
    assert len(runtime.fake_webex.posts) == 1


def test_ambiguous_post_is_not_retried(runtime) -> None:
    runtime.fake_webex.messages["ambiguous"] = group_message("ambiguous")
    runtime.fake_webex.post_error = AmbiguousWebexAPIError(
        "synthetic read timeout",
        code="webex_ambiguous_write",
    )
    assert service(runtime).process(payload("ambiguous")).status == "ambiguous-post"
    with runtime.session_factory() as session:
        assert session.get(ProcessedMessage, "ambiguous").status == "ambiguous"
    runtime.fake_webex.post_error = None
    assert service(runtime).process(payload("ambiguous")).status == "duplicate-message"
    assert runtime.fake_webex.posts == []


@pytest.mark.parametrize(
    "authoritative_message",
    [
        {
            "id": "missing-routing",
            "mentionedPeople": ["bot-person"],
            "markdown": "How should I protect an access token?",
        },
        group_message("different-id"),
    ],
)
def test_webhook_envelope_cannot_supply_or_override_routing(runtime, authoritative_message) -> None:
    message_id = (
        "missing-routing" if authoritative_message["id"] == "missing-routing" else "requested"
    )
    runtime.fake_webex.messages[message_id] = authoritative_message
    untrusted = payload(message_id, room_id="allowed-space")
    untrusted["data"].update({"roomType": "group", "personId": "person-1"})

    assert service(runtime).process(untrusted).status == "ignored-invalid"
    assert runtime.fake_webex.posts == []


def test_invalid_payload_is_ignored(runtime) -> None:
    assert service(runtime).process({"resource": "messages", "event": "created"}).status == (
        "ignored-invalid"
    )
    assert (
        service(runtime)
        .process({"resource": "messages", "event": "created", "data": "invalid"})
        .status
        == "ignored-invalid"
    )
