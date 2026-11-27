"""
Tests for issue #4: error handling must not hide the real validation failure,
and errors must reach the MCP client as clear tool errors (isError=True).
The errors of api_call are tested in test_api_call.py (issue #5).
"""
import logging
import re
from unittest.mock import patch

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_server import main
from mcp_server.main import (
    get_location_codes,
    get_low_level_occupation_codes,
    get_medium_level_occupation_codes,
    normalise_occupation_code,
)

medium_mock_data = [
    {"code": "11", "preferredLabel": "Upseerit"},
    {"code": "21", "preferredLabel": "Aliupseerit"},
]
low_mock_data = [
    {"code": "111", "preferredLabel": "Upseerit"},
    {"code": "211", "preferredLabel": "Aliupseerit"},
]


# normalise_occupation_code: the error message names the argument, the format and the value

HIGH_EXPECTED = "high_level_code must be an occupation code with 1 digit(s), for example '1'"
MEDIUM_EXPECTED = "medium_level_code must be an occupation code with 2 digit(s), for example '01'"


@pytest.mark.parametrize("value, digits, expected", [
    (1, 1, "1"), ("9", 1, "9"), (" 5 ", 1, "5"), (0, 1, "0"),
    (10, 2, "10"), ("99", 2, "99"), (1, 2, "01"), ("05", 2, "05"),
])
def test_normalise_occupation_code_valid(value, digits, expected):
    assert normalise_occupation_code(value, digits=digits, name="code") == expected


@pytest.mark.parametrize("value", ["abc", "", "2.5", None, [1]])
def test_normalise_occupation_code_not_a_code_message(value):
    with pytest.raises(ValueError) as excinfo:
        normalise_occupation_code(value, digits=1, name="high_level_code")
    message = str(excinfo.value)
    assert HIGH_EXPECTED in message
    assert f"got {value!r}." in message
    # The low-level int() message must not leak through as the only explanation
    assert "invalid literal" not in message


@pytest.mark.parametrize("value", [10, -1, "120", "01"])
def test_normalise_occupation_code_full_message(value):
    with pytest.raises(ValueError) as excinfo:
        normalise_occupation_code(value, digits=1, name="high_level_code")
    assert str(excinfo.value) == (
        f"{HIGH_EXPECTED}, got {value!r}. Give the code as a string to keep a leading zero."
    )


@pytest.mark.parametrize("value", [True, False])
def test_normalise_occupation_code_rejects_bool(value):
    with pytest.raises(ValueError, match=f"got {value!r}"):
        normalise_occupation_code(value, digits=1, name="code")


# Tool functions: error message is not empty and tells what is allowed

def test_medium_level_out_of_range_message_not_empty():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=medium_mock_data):
        with pytest.raises(ValueError, match=re.escape(f"{HIGH_EXPECTED}, got 120.")):
            get_medium_level_occupation_codes(120)


def test_medium_level_two_digit_code_rejected():
    # "0" is armed forces and is valid (issue #3). A two-digit code is not a high level code.
    with patch("mcp_server.main.read_file_contents_tojson", return_value=medium_mock_data):
        with pytest.raises(ValueError, match="got '01'"):
            get_medium_level_occupation_codes("01")


def test_medium_level_non_numeric_message():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=medium_mock_data):
        with pytest.raises(ValueError, match=re.escape(f"{HIGH_EXPECTED}, got 'abc'.")):
            get_medium_level_occupation_codes("abc")


def test_low_level_out_of_range_message_not_empty():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=low_mock_data):
        # 9 is valid now ("09"), so use a code with too many digits
        with pytest.raises(ValueError, match=re.escape(f"{MEDIUM_EXPECTED}, got 120.")):
            get_low_level_occupation_codes(120)


def test_low_level_non_numeric_message():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=low_mock_data):
        with pytest.raises(ValueError, match=re.escape(f"{MEDIUM_EXPECTED}, got 'x1'.")):
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
                get_low_level_occupation_codes("abc")
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
    assert f"{HIGH_EXPECTED}, got 42." in result_text(result)


def test_mcp_client_gets_clear_error_for_non_numeric_low_code():
    result = call_tool_via_client("get_low_level_occupation_codes", {"medium_level_code": "abc"})
    assert result.isError is True
    assert f"{MEDIUM_EXPECTED}, got 'abc'." in result_text(result)


def test_mcp_client_armed_forces_group_is_not_error():
    # Issue #3: "0" (armed forces) is a valid high level code
    result = call_tool_via_client("get_medium_level_occupation_codes", {"high_level_code": "0"})
    assert result.isError is False
    assert "01" in result_text(result)


def test_mcp_client_valid_call_is_not_error():
    result = call_tool_via_client("get_medium_level_occupation_codes", {"high_level_code": 2})
    assert result.isError is False
    assert "21" in result_text(result)
