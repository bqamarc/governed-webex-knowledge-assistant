from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{12,}")
SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|secret|password)"
    r"\s*[:=]\s*['\"]?[^\s,'\";]{6,}"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
IP_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IDENTIFIER_RE = re.compile(
    r"(?i)\b(customer|tenant|organization|account|subscription|case)"
    r"(?:[_ -]?(?:name|id|number))?\s*[:=#]\s*[A-Za-z0-9][A-Za-z0-9._-]{3,}"
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    counts: dict[str, int]


def redact_text(value: str | None) -> RedactionResult:
    text = value or ""
    counts: Counter[str] = Counter()

    def replace(pattern: re.Pattern[str], replacement: str, category: str) -> None:
        nonlocal text
        text, count = pattern.subn(replacement, text)
        counts[category] += count

    replace(PRIVATE_KEY_RE, "[REDACTED PRIVATE KEY]", "private_key")
    replace(JWT_RE, "[REDACTED TOKEN]", "token")
    replace(BEARER_RE, "Bearer [REDACTED TOKEN]", "token")
    replace(SECRET_RE, "[REDACTED CREDENTIAL]", "credential")
    replace(IP_RE, "[REDACTED IP]", "network_identifier")
    replace(IDENTIFIER_RE, "[REDACTED IDENTIFIER]", "organization_identifier")

    text, email_count = EMAIL_RE.subn("[REDACTED EMAIL]", text)
    counts["email"] += email_count
    return RedactionResult(text=text, counts={key: value for key, value in counts.items() if value})
