from __future__ import annotations

import hashlib
import hmac

from webex_knowledge_assistant.security import delivery_id, verify_webhook_signature


def test_signature_verifies_exact_raw_body() -> None:
    body = b'{"event":"created"}'
    signature = hmac.new(b"secret", body, hashlib.sha1).hexdigest()
    assert verify_webhook_signature(body, signature, "secret")
    assert not verify_webhook_signature(body + b" ", signature, "secret")
    assert not verify_webhook_signature(body, None, "secret")
    assert not verify_webhook_signature(body, signature, "")


def test_delivery_id_is_stable_and_body_specific() -> None:
    assert delivery_id(b"one") == delivery_id(b"one")
    assert delivery_id(b"one") != delivery_id(b"two")
    assert delivery_id(b"one").startswith("sha256:")
