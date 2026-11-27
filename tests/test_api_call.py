import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_server.main import (
    API_URL,
    API_TIMEOUT_SECONDS,
    API_call,
    APIHTTPStatusError,
    APIInvalidResponseError,
    APINetworkError,
    APITimeoutError,
    TyomarkkinatoriAPIError,
    api_call,
    mcp,
)

REAL_ASYNC_CLIENT = httpx.AsyncClient

OK_PAYLOAD = {"content": [{"id": "abc", "title": "Ohjelmistokehittäjä"}], "totalElements": 1}


def patch_client(handler):
    """Make api_call use an AsyncClient with a mock transport that calls handler(request)."""
    transport = httpx.MockTransport(handler)
    return patch(
        "mcp_server.main.AsyncClient",
        side_effect=lambda *args, **kwargs: REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs),
    )


def run_api_call(handler, municipalities=None, occupations=None):
    request_input = API_call(municipalities=municipalities or [], occupations=occupations or [])
    with patch_client(handler):
        return asyncio.run(api_call(request_input))


def raise_exception(exc_type, message="boom"):
    def handler(request):
        raise exc_type(message, request=request)
    return handler


def status_response(status, body="", content_type="application/json"):
    def handler(request):
        return httpx.Response(status, text=body, headers={"Content-Type": content_type})
    return handler


# --- Success path ---------------------------------------------------------

def test_success_returns_json():
    result = run_api_call(lambda request: httpx.Response(200, json=OK_PAYLOAD))
    assert result == OK_PAYLOAD


def test_success_request_body():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_PAYLOAD)

    run_api_call(handler, municipalities=[91, "49"], occupations=["2512"])

    assert captured["url"] == API_URL
    assert captured["method"] == "POST"
    assert captured["body"]["filters"]["municipalities"] == ["091", "049"]
    assert captured["body"]["filters"]["iscoNotations"] == ["2512"]


# --- Failures never return None ---------------------------------------------

@pytest.mark.parametrize("handler", [
    raise_exception(httpx.ReadTimeout),
    raise_exception(httpx.ConnectError),
    status_response(500),
    status_response(400),
    status_response(200, body="<html>not json</html>", content_type="text/html"),
])
def test_failure_raises_tool_error_not_none(handler):
    # Before the fix api_call returned None for every failure.
    with pytest.raises(TyomarkkinatoriAPIError) as exc_info:
        run_api_call(handler)
    assert isinstance(exc_info.value, ToolError)
    assert str(exc_info.value)


# --- Timeout ---------------------------------------------------------------

@pytest.mark.parametrize("exc_type", [
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
])
def test_timeout_raises_timeout_error(exc_type):
    with pytest.raises(APITimeoutError) as exc_info:
        run_api_call(raise_exception(exc_type))
    message = str(exc_info.value)
    assert "timeout" in message.lower()
    assert str(API_TIMEOUT_SECONDS) in message
    assert exc_type.__name__ in message
    assert isinstance(exc_info.value.__cause__, exc_type)


def test_timeout_is_not_reported_as_network_error():
    with pytest.raises(TyomarkkinatoriAPIError) as exc_info:
        run_api_call(raise_exception(httpx.ReadTimeout))
    assert not isinstance(exc_info.value, APINetworkError)


# --- HTTP status -----------------------------------------------------------

@pytest.mark.parametrize("status", [400, 404, 422, 429])
def test_client_error_status(status):
    body = '{"message": "Invalid municipality code"}'
    with pytest.raises(APIHTTPStatusError) as exc_info:
        run_api_call(status_response(status, body=body))
    error = exc_info.value
    message = str(error)
    assert error.status_code == status
    assert f"HTTP {status}" in message
    assert "codes" in message  # hint to check the codes
    assert "Invalid municipality code" in message  # API validation message is passed on
    assert isinstance(error.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_error_status(status):
    with pytest.raises(APIHTTPStatusError) as exc_info:
        run_api_call(status_response(status, body="Service Unavailable"))
    error = exc_info.value
    message = str(error)
    assert error.status_code == status
    assert f"HTTP {status}" in message
    assert "try again later" in message


def test_status_error_without_body():
    with pytest.raises(APIHTTPStatusError) as exc_info:
        run_api_call(status_response(503, body=""))
    assert "Response body" not in str(exc_info.value)


def test_status_error_long_body_is_truncated():
    long_body = "x" * 5000
    with pytest.raises(APIHTTPStatusError) as exc_info:
        run_api_call(status_response(500, body=long_body))
    message = str(exc_info.value)
    assert "(truncated)" in message
    assert len(message) < 1000


# --- Network ---------------------------------------------------------------

@pytest.mark.parametrize("exc_type", [
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
])
def test_network_error(exc_type):
    with pytest.raises(APINetworkError) as exc_info:
        run_api_call(raise_exception(exc_type, "Name or service not known"))
    message = str(exc_info.value)
    assert "network error" in message
    assert exc_type.__name__ in message
    assert "Name or service not known" in message
    assert isinstance(exc_info.value.__cause__, exc_type)


# --- Invalid response ------------------------------------------------------

def test_invalid_json_response():
    with pytest.raises(APIInvalidResponseError) as exc_info:
        run_api_call(status_response(200, body="<html>maintenance</html>", content_type="text/html"))
    message = str(exc_info.value)
    assert "not valid JSON" in message
    assert "maintenance" in message


# --- End to end through an MCP client session ------------------------------

def call_tool_via_mcp_client(handler, arguments):
    async def run():
        async with create_connected_server_and_client_session(mcp._mcp_server) as client:
            return await client.call_tool("api_call", arguments)

    with patch_client(handler):
        return asyncio.run(run())


def result_text(result):
    return " ".join(item.text for item in result.content if item.type == "text")


@pytest.mark.parametrize("handler, expected_text", [
    (raise_exception(httpx.ReadTimeout), "timeout"),
    (status_response(503, body="down"), "HTTP 503"),
    (status_response(400, body="bad code"), "HTTP 400"),
    (raise_exception(httpx.ConnectError), "network error"),
    (status_response(200, body="nope", content_type="text/plain"), "not valid JSON"),
])
def test_mcp_client_receives_error_result(handler, expected_text):
    result = call_tool_via_mcp_client(handler, {"input": {"municipalities": ["91"], "occupations": []}})
    assert result.isError is True
    assert expected_text in result_text(result)


def test_mcp_client_receives_success_result():
    result = call_tool_via_mcp_client(
        lambda request: httpx.Response(200, json=OK_PAYLOAD),
        {"input": {"municipalities": ["91"], "occupations": []}},
    )
    assert result.isError is False
    assert "Ohjelmistokehittäjä" in result_text(result)


def test_mcp_client_receives_validation_error():
    # Input validation (more than 10 municipalities) also reaches the client as an error.
    def handler(request):
        raise AssertionError("API must not be called when the input is not valid")

    result = call_tool_via_mcp_client(handler, {"input": {"municipalities": list(range(11))}})
    assert result.isError is True
    assert "municipalities" in result_text(result)
