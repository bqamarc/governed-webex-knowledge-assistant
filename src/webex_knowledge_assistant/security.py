from __future__ import annotations

import hashlib
import hmac


def verify_webhook_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Verify the Webex HMAC-SHA1 compatibility signature over unmodified bytes."""

    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def delivery_id(raw_body: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw_body).hexdigest()}"
