"""
Tests for issue #4: error handling must not hide the real validation failure,
and errors must reach the MCP client as clear tool errors (isError=True).
"""
import logging
from unittest.mock import patch

import anyio
import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_server import main
from mcp_server.main import (
    API_call,
    api_call,
    get_location_codes,
    get_low_level_occupation_codes,
    get_medium_level_occupation_codes,
    parse_code_in_range,
)

medium_mock_data = [
    {"code": "11", "preferredLabel": "Upseerit"},
    {"code": "21", "preferredLabel": "Aliupseerit"},
]
low_mock_data = [
    {"code": "111", "preferredLabel": "Upseerit"},
    {"code": "211", "preferredLabel": "Aliupseerit"},
]


# parse_code_in_range

@pytest.mark.parametrize("value, expected", [(1, 1), ("9", 9), (" 5 ", 5), (10, 10), ("99", 99)])
def test_parse_code_in_range_valid(value, expected):
    low, high = (1, 9) if expected < 10 else (10, 99)
    assert parse_code_in_range(value, name="code", minimum=low, maximum=high) == expected


@pytest.mark.parametrize("value", ["abc", "", "2.5", None, [1]])
def test_parse_code_in_range_not_integer_message(value):
    with pytest.raises(ValueError) as excinfo:
        parse_code_in_range(value, name="high_level_code", minimum=1, maximum=9)
    message = str(excinfo.value)
    assert "high_level_code must be an integer from 1 to 9" in message
    assert repr(value) in message
    # The low-level int() message must not leak through as the only explanation
    assert "invalid literal" not in message


@pytest.mark.parametrize("value", [0, 10, -1, "120"])
def test_parse_code_in_range_out_of_range_message(value):
    with pytest.raises(ValueError) as excinfo:
        parse_code_in_range(value, name="high_level_code", minimum=1, maximum=9)
    assert str(excinfo.value) == f"high_level_code must be an integer from 1 to 9, got {int(value)}."


@pytest.mark.parametrize("value", [True, False])
def test_parse_code_in_range_rejects_bool(value):
    with pytest.raises(ValueError, match="got boolean"):
        parse_code_in_range(value, name="code", minimum=0, maximum=9)


# Tool functions: error message is not empty and tells what is allowed

def test_medium_level_out_of_range_message_not_empty():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=medium_mock_data):
        with pytest.raises(ValueError, match=r"high_level_code must be an integer from 1 to 9, got 120\."):
            get_medium_level_occupation_codes(120)


def test_medium_level_zero_rejected():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=medium_mock_data):
        with pytest.raises(ValueError, match="got 0"):
            get_medium_level_occupation_codes(0)


def test_medium_level_non_numeric_message():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=medium_mock_data):
        with pytest.raises(ValueError, match=r"high_level_code must be an integer from 1 to 9, got 'abc'\."):
            get_medium_level_occupation_codes("abc")


def test_low_level_out_of_range_message_not_empty():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=low_mock_data):
        with pytest.raises(ValueError, match=r"medium_level_code must be an integer from 10 to 99, got 9\."):
            get_low_level_occupation_codes(9)


def test_low_level_non_numeric_message():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=low_mock_data):
        with pytest.raises(ValueError, match=r"medium_level_code must be an integer from 10 to 99, got 'x1'\."):
            get_low_level_occupation_codes("x1")


def test_low_level_input_with_whitespace_matches():
    # Before the fix, int(" 21 ") passed but the filter compared against " 21 " and found nothing
    with patch("mcp_server.main.read_file_contents_tojson", return_value=low_mock_data):
        assert get_low_level_occupation_codes(" 21 ") == [{"code": "211", "preferredLabel": "Aliupseerit"}]


def test_low_level_log_names_correct_function(caplog):
    # Before the fix, the low-level tool logged the name of the medium-level tool
    with patch("mcp_server.main.read_file_contents_tojson", return_value=low_mock_data):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ValueError):
                get_low_level_occupation_codes(5)
    messages = [r.getMessage() for r in caplog.records]
    assert any("get_low_level_occupation_codes" in m for m in messages)
    assert not any("get_medium_level_occupation_codes" in m for m in messages)


def test_invalid_input_is_logged_as_warning_not_error(caplog):
    with patch("mcp_server.main.read_file_contents_tojson", return_value=medium_mock_data):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ValueError):
                get_medium_level_occupation_codes(42)
    assert caplog.records
    assert all(r.levelno == logging.WARNING for r in caplog.records)


def test_location_codes_type_error_message():
    with pytest.raises(TypeError, match="first_letters_of_the_city must be a string, got int"):
        get_location_codes(1)


def test_file_errors_are_not_converted_to_value_error():
    # Server-side problems must keep their own exception type, not look like bad input
    with patch("mcp_server.main.read_file_contents_tojson", side_effect=FileNotFoundError("missing.json")):
        with pytest.raises(FileNotFoundError):
            get_medium_level_occupation_codes(2)


# api_call: errors are raised as ToolError with a clear message, not returned as None

def run_api_call_with_transport(handler, payload=None):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_client(transport=transport)

    payload = payload or API_call(municipalities=[91], occupations=["2512"])
    with patch("mcp_server.main.AsyncClient", side_effect=client_factory):
        return anyio.run(api_call, payload)


def test_api_call_success_returns_json_and_pads_municipality():
    seen = {}

    def handler(request: httpx.Request):
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"id": 1}]})

    result = run_api_call_with_transport(handler)
    assert result == {"content": [{"id": 1}]}
    assert seen["body"]["filters"]["municipalities"] == ["091"]
    assert seen["body"]["filters"]["iscoNotations"] == ["2512"]


def test_api_call_http_error_raises_tool_error():
    def handler(request):
        return httpx.Response(400, text="bad request")

    with pytest.raises(ToolError, match="returned HTTP 400"):
        run_api_call_with_transport(handler)


def test_api_call_server_error_raises_tool_error():
    def handler(request):
        return httpx.Response(503, text="unavailable")

    with pytest.raises(ToolError, match="returned HTTP 503"):
        run_api_call_with_transport(handler)


def test_api_call_timeout_raises_tool_error():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ToolError, match="did not respond in time"):
        run_api_call_with_transport(handler)


def test_api_call_connection_error_raises_tool_error():
    def handler(request):
        raise httpx.ConnectError("name resolution failed", request=request)

    with pytest.raises(ToolError, match="Could not connect to Työmarkkinatori API: ConnectError"):
        run_api_call_with_transport(handler)


def test_api_call_invalid_json_raises_tool_error():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(ToolError, match="not valid JSON"):
        run_api_call_with_transport(handler)


# End to end through an MCP client session: the agent gets isError=True and the clear message

def call_tool_via_client(name: str, arguments: dict):
    async def run():
        async with create_connected_server_and_client_session(main.mcp._mcp_server) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(run)


def result_text(result) -> str:
    return " ".join(c.text for c in result.content if getattr(c, "type", None) == "text")


def test_mcp_client_gets_clear_error_for_out_of_range_medium_code():
    result = call_tool_via_client("get_medium_level_occupation_codes", {"high_level_code": 42})
    assert result.isError is True
    assert "high_level_code must be an integer from 1 to 9, got 42." in result_text(result)


def test_mcp_client_gets_clear_error_for_non_numeric_low_code():
    result = call_tool_via_client("get_low_level_occupation_codes", {"medium_level_code": "abc"})
    assert result.isError is True
    assert "medium_level_code must be an integer from 10 to 99, got 'abc'." in result_text(result)


def test_mcp_client_valid_call_is_not_error():
    result = call_tool_via_client("get_medium_level_occupation_codes", {"high_level_code": 2})
    assert result.isError is False
    assert "21" in result_text(result)


def test_mcp_client_gets_tool_error_for_api_failure():
    def handler(request):
        return httpx.Response(502)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    with patch("mcp_server.main.AsyncClient", side_effect=lambda *a, **k: real_client(transport=transport)):
        result = call_tool_via_client("api_call", {"input": {"municipalities": [91], "occupations": []}})
    assert result.isError is True
    assert "returned HTTP 502" in result_text(result)
