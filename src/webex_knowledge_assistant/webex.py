from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

MAX_RETRY_AFTER_SECONDS = 300


class WebexAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class RetryableWebexAPIError(WebexAPIError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.retry_after_seconds = retry_after_seconds


class PermanentWebexAPIError(WebexAPIError):
    pass


class AmbiguousWebexAPIError(WebexAPIError):
    """The request may have reached Webex, so automatically replaying is unsafe."""


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = max(0, int((retry_at - datetime.now(UTC)).total_seconds()))
    return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))


class WebexClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://webexapis.com/v1",
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=20.0)

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        method = method.upper()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.client.request(
                method,
                f"{self.base_url}/{path.lstrip('/')}",
                headers=headers,
                **kwargs,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise RetryableWebexAPIError(
                f"Webex request was not sent: {type(exc).__name__}",
                code="webex_connect_failure",
            ) from exc
        except httpx.TransportError as exc:
            error_type = type(exc).__name__
            if method in {"GET", "HEAD"}:
                raise RetryableWebexAPIError(
                    f"Webex read request failed: {error_type}",
                    code="webex_read_failure",
                ) from exc
            raise AmbiguousWebexAPIError(
                f"Webex write outcome is unknown: {error_type}",
                code="webex_ambiguous_write",
            ) from exc

        status_code = response.status_code
        if status_code == 429:
            raise RetryableWebexAPIError(
                "Webex returned HTTP 429",
                code="webex_http_429",
                retry_after_seconds=_retry_after_seconds(response.headers.get("retry-after")),
            )
        if 500 <= status_code < 600:
            if method in {"GET", "HEAD"}:
                raise RetryableWebexAPIError(
                    f"Webex returned HTTP {status_code}",
                    code=f"webex_http_{status_code}",
                )
            raise AmbiguousWebexAPIError(
                f"Webex write outcome after HTTP {status_code} is unknown",
                code=f"webex_ambiguous_http_{status_code}",
            )
        if status_code == 408 and method not in {"GET", "HEAD"}:
            raise AmbiguousWebexAPIError(
                "Webex write outcome after HTTP 408 is unknown",
                code="webex_ambiguous_http_408",
            )
        if status_code == 408 and method in {"GET", "HEAD"}:
            raise RetryableWebexAPIError(
                "Webex returned HTTP 408",
                code="webex_http_408",
            )
        if method == "GET" and status_code == 404:
            raise RetryableWebexAPIError(
                "Webex message is not available yet",
                code="webex_http_404",
            )
        if not 200 <= status_code < 300:
            raise PermanentWebexAPIError(
                f"Webex returned HTTP {status_code}",
                code=f"webex_http_{status_code}",
            )
        if not response.content:
            return {}
        try:
            value = response.json()
        except ValueError as exc:
            if method in {"GET", "HEAD"}:
                raise RetryableWebexAPIError(
                    "Webex returned an invalid JSON response",
                    code="webex_invalid_response",
                ) from exc
            return {}
        if not isinstance(value, dict):
            if method in {"GET", "HEAD"}:
                raise RetryableWebexAPIError(
                    "Webex returned a non-object response",
                    code="webex_invalid_response",
                )
            return {}
        return value

    def get_message(self, message_id: str) -> dict:
        return self._request("GET", f"messages/{message_id}")

    def post_message(self, *, room_id: str, markdown: str, parent_id: str | None = None) -> dict:
        payload: dict[str, str] = {"roomId": room_id, "markdown": markdown}
        if parent_id:
            payload["parentId"] = parent_id
        return self._request("POST", "messages", json=payload)
