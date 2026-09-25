from __future__ import annotations

import httpx
import pytest

from webex_knowledge_assistant.webex import (
    AmbiguousWebexAPIError,
    PermanentWebexAPIError,
    RetryableWebexAPIError,
    WebexAPIError,
    WebexClient,
)


def test_webex_client_uses_bearer_and_expected_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "result"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    webex = WebexClient("synthetic-token", client=client)
    assert webex.get_message("message-1") == {"id": "result"}
    webex.post_message(room_id="space-1", markdown="Hello", parent_id="parent-1")
    assert requests[0].headers["authorization"] == "Bearer synthetic-token"
    assert requests[1].url.path == "/v1/messages"
    assert b'"parentId":"parent-1"' in requests[1].content
    webex.close()


def test_webex_client_sanitizes_errors() -> None:
    failing = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(403, json={"secret": "x"}))
    )
    webex = WebexClient("synthetic-token", client=failing)
    with pytest.raises(WebexAPIError, match="HTTP 403") as error:
        webex.get_message("message-1")
    assert "secret" not in str(error.value)
    webex.close()


def test_webex_client_classifies_429_and_bounds_retry_after() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"Retry-After": "999"})
        )
    )
    webex = WebexClient("synthetic-token", client=client)
    with pytest.raises(RetryableWebexAPIError) as error:
        webex.post_message(room_id="space-1", markdown="Hello")
    assert error.value.code == "webex_http_429"
    assert error.value.retry_after_seconds == 300
    webex.close()


def test_webex_client_classifies_permanent_4xx() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(400, json={"message": "x"}))
    )
    webex = WebexClient("synthetic-token", client=client)
    with pytest.raises(PermanentWebexAPIError) as error:
        webex.post_message(room_id="space-1", markdown="Hello")
    assert error.value.code == "webex_http_400"
    webex.close()


def test_connect_failure_is_safe_to_retry() -> None:
    def fail_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connect failure", request=request)

    webex = WebexClient(
        "synthetic-token",
        client=httpx.Client(transport=httpx.MockTransport(fail_connect)),
    )
    with pytest.raises(RetryableWebexAPIError, match="not sent") as error:
        webex.post_message(room_id="space-1", markdown="Hello")
    assert error.value.code == "webex_connect_failure"
    webex.close()


def test_post_read_timeout_is_ambiguous_but_get_is_retryable() -> None:
    def fail_read(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic read timeout", request=request)

    webex = WebexClient(
        "synthetic-token",
        client=httpx.Client(transport=httpx.MockTransport(fail_read)),
    )
    with pytest.raises(AmbiguousWebexAPIError) as post_error:
        webex.post_message(room_id="space-1", markdown="Hello")
    assert post_error.value.code == "webex_ambiguous_write"
    with pytest.raises(RetryableWebexAPIError) as get_error:
        webex.get_message("message-1")
    assert get_error.value.code == "webex_read_failure"
    webex.close()


def test_post_5xx_is_ambiguous_but_get_5xx_is_retryable() -> None:
    webex = WebexClient(
        "synthetic-token",
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))),
    )
    with pytest.raises(AmbiguousWebexAPIError) as post_error:
        webex.post_message(room_id="space-1", markdown="Hello")
    assert post_error.value.code == "webex_ambiguous_http_503"
    with pytest.raises(RetryableWebexAPIError) as get_error:
        webex.get_message("message-1")
    assert get_error.value.code == "webex_http_503"
    webex.close()
