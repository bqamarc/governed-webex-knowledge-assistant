from webex_knowledge_assistant.redaction import redact_text


def test_sensitive_values_are_redacted() -> None:
    result = redact_text(
        "email user@example.com token=very-secret-value host 192.0.2.10 customer_id=customer-1234"
    )
    assert "user@example.com" not in result.text
    assert "very-secret-value" not in result.text
    assert "192.0.2.10" not in result.text
    assert "customer-1234" not in result.text
    assert result.counts["email"] == 1
    assert result.counts["credential"] == 1


def test_empty_redaction_is_safe() -> None:
    assert redact_text(None).text == ""
    assert redact_text(None).counts == {}
