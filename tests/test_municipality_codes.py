"""
Tests for municipality codes (issue #6).

The Työmarkkinatori retrieval interface instructions (noutorajapintaohje.pdf,
chapters 5.1 and 7.7) say that the municipality filter uses the municipality
codes of Statistics Finland ("Kunnat 2022"). These codes have three digits,
for example "837" for Tampere and "091" for Helsinki.
"""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from mcp_server.main import (
    MUNICIPALITIES_FILE,
    API_call,
    api_call,
    get_location_codes,
    known_municipality_codes,
    normalise_municipality_code,
)
from tests.test_api_call import call_tool_via_mcp_client, patch_client, result_text

OK_PAYLOAD = {"content": [], "totalElements": 0}


def load_municipalities():
    with MUNICIPALITIES_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def sent_municipalities(municipalities):
    """Run api_call with a mock transport and return the municipality filter of the request body."""
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_PAYLOAD)

    request_input = API_call(municipalities=municipalities)
    with patch_client(handler):
        asyncio.run(api_call(request_input))
    return captured["body"]["filters"]["municipalities"]


# --- Data file -------------------------------------------------------------

def test_data_file_codes_have_three_digits():
    for item in load_municipalities():
        code = item["code"]
        assert isinstance(code, str), item
        assert len(code) == 3 and code.isascii() and code.isdigit(), item


def test_data_file_codes_are_unique():
    codes = [i["code"] for i in load_municipalities()]
    assert len(codes) == len(set(codes))


def test_data_file_has_309_municipalities():
    # Kunnat 2022 classification has 309 municipalities.
    assert len(load_municipalities()) == 309


@pytest.mark.parametrize("name, code", [
    ("Tampere", "837"),  # example in the API instructions
    ("Helsinki", "091"),
    ("Espoo", "049"),
    ("Akaa", "020"),
    ("Alajärvi", "005"),
    ("Turku", "853"),
])
def test_data_file_known_codes(name, code):
    by_name = {i["classificationName"]: i["code"] for i in load_municipalities()}
    assert by_name[name] == code


def test_get_location_codes_returns_three_digit_code():
    assert get_location_codes("Helsinki") == [{"code": "091", "classificationName": "Helsinki"}]


def test_get_location_codes_result_is_accepted_by_api_call():
    codes = [i["code"] for i in get_location_codes("Hel")]
    assert codes
    assert sent_municipalities(codes) == codes


def test_known_codes_match_data_file():
    assert known_municipality_codes() == frozenset(i["code"] for i in load_municipalities())


# --- normalise_municipality_code -------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (91, "091"),
    ("91", "091"),
    ("091", "091"),
    (" 091 ", "091"),
    (5, "005"),
    ("5", "005"),
    ("05", "005"),
    ("837", "837"),
    (837, "837"),
])
def test_normalise_valid(value, expected):
    assert normalise_municipality_code(value) == expected


@pytest.mark.parametrize("value", [
    "",
    " ",
    "abc",
    "9a",
    "-5",
    -5,
    "1234",
    1234,
    "0091",
    "09.1",
    "٩١",  # Arabic-Indic digits: isdigit() is True, but not ASCII
    True,
    False,
])
def test_normalise_rejects_bad_format(value):
    with pytest.raises(ValueError, match="municipality code"):
        normalise_municipality_code(value)


@pytest.mark.parametrize("value", ["000", 0, "1", "999", 999, "100"])
def test_normalise_rejects_unknown_code(value):
    # Format is correct, but there is no such municipality.
    with pytest.raises(ValueError, match="not a known municipality code"):
        normalise_municipality_code(value)


# --- API_call model ----------------------------------------------------------

def test_model_normalises_codes():
    assert API_call(municipalities=[91, "49", "837"]).municipalities == ["091", "049", "837"]


def test_model_empty_list_is_valid():
    assert API_call().municipalities == []


def test_model_reports_all_invalid_codes():
    with pytest.raises(ValidationError) as exc_info:
        API_call(municipalities=["091", "abc", "1234", "999"])
    message = str(exc_info.value)
    assert "'abc'" in message
    assert "'1234'" in message
    assert "'999'" in message
    assert "get_location_codes" in message


@pytest.mark.parametrize("value", [True, False])
def test_model_rejects_bool(value):
    with pytest.raises(ValidationError, match="municipality code"):
        API_call(municipalities=[value])


def test_model_still_limits_to_ten_codes():
    with pytest.raises(ValidationError, match="municipalities"):
        API_call(municipalities=["091"] * 11)


# --- Request body ------------------------------------------------------------

@pytest.mark.parametrize("given, sent", [
    ([91], ["091"]),
    (["5", 20, "837"], ["005", "020", "837"]),
    ([], []),
])
def test_request_body_has_three_digit_codes(given, sent):
    assert sent_municipalities(given) == sent


# --- MCP client ------------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", "1234", "999", True])
def test_mcp_client_gets_error_and_api_is_not_called(bad):
    def handler(request):
        raise AssertionError("API must not be called when a municipality code is not valid")

    result = call_tool_via_mcp_client(handler, {"input": {"municipalities": ["091", bad]}})
    assert result.isError is True
    assert "municipality code" in result_text(result)
