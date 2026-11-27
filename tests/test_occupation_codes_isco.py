"""
Occupation codes are ISCO-08 codes. The codes for armed forces start with "0"
("0", "01", "011", "0110"), so a code is a string and the leading zero is part
of the code. The Tyomarkkinatori search API treats "03" and "3" as different
codes, and a short code also finds the jobs that have longer codes starting
with it ("21" finds "2149").
"""
import json
from unittest.mock import patch

import pytest

from mcp_server.main import (
    OCCUPATIONS_HIGH_LEVEL_FILE,
    OCCUPATIONS_MEDIUM_LEVEL_FILE,
    OCCUPATIONS_LOW_LEVEL_FILE,
    OCCUPATIONS_LEVEL4_FILE,
    get_medium_level_occupation_codes,
    get_low_level_occupation_codes,
)

LEVEL_FILES = {
    1: OCCUPATIONS_HIGH_LEVEL_FILE,
    2: OCCUPATIONS_MEDIUM_LEVEL_FILE,
    3: OCCUPATIONS_LOW_LEVEL_FILE,
    4: OCCUPATIONS_LEVEL4_FILE,
}
COMBINED_FILE = OCCUPATIONS_HIGH_LEVEL_FILE.parent / "ammatit.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# Data files

@pytest.mark.parametrize("digits", LEVEL_FILES)
def test_codes_have_level_length_and_are_unique(digits):
    codes = [item["code"] for item in load(LEVEL_FILES[digits])]
    assert [c for c in codes if not (c.isdigit() and len(c) == digits)] == []
    assert sorted(c for c in set(codes) if codes.count(c) > 1) == []


@pytest.mark.parametrize("digits", [2, 3, 4])
def test_every_code_has_parent_on_level_above(digits):
    parents = {item["code"] for item in load(LEVEL_FILES[digits - 1])}
    orphans = [item["code"] for item in load(LEVEL_FILES[digits]) if item["code"][:-1] not in parents]
    assert orphans == []


def test_armed_forces_codes_keep_leading_zero():
    expected = {
        1: {"0"},
        2: {"01", "02", "03"},
        3: {"011", "021", "031"},
        4: {"0110", "0210", "0310"},
    }
    for digits, codes in expected.items():
        assert codes <= {item["code"] for item in load(LEVEL_FILES[digits])}


def test_combined_file_matches_level_files():
    def key(item):
        return item["code"], item["preferredLabel"]

    from_levels = [item for path in LEVEL_FILES.values() for item in load(path)]
    assert sorted(map(key, load(COMBINED_FILE))) == sorted(map(key, from_levels))


# Medium level codes

MEDIUM_CODES = [
    {"code": "01", "preferredLabel": "Upseerit"},
    {"code": "02", "preferredLabel": "Aliupseerit"},
    {"code": "11", "preferredLabel": "Johtajat, ylimmät virkamiehet ja järjestöjen johtajat"},
    {"code": "21", "preferredLabel": "Luonnontieteiden ja tekniikan erityisasiantuntijat"},
]


@pytest.mark.parametrize("high_level_code", ["0", 0, " 0 "])
def test_medium_level_armed_forces(high_level_code):
    with patch("mcp_server.main.read_file_contents_tojson", return_value=MEDIUM_CODES):
        result = get_medium_level_occupation_codes(high_level_code)
    assert [item["code"] for item in result] == ["01", "02"]


def test_medium_level_does_not_mix_armed_forces_into_group_1():
    with patch("mcp_server.main.read_file_contents_tojson", return_value=MEDIUM_CODES):
        result = get_medium_level_occupation_codes("1")
    assert [item["code"] for item in result] == ["11"]


@pytest.mark.parametrize("high_level_code", ["10", "01", "-1", "", True, 1.0])
def test_medium_level_rejects_code_that_is_not_one_digit(high_level_code):
    with patch("mcp_server.main.read_file_contents_tojson", return_value=MEDIUM_CODES):
        with pytest.raises(ValueError, match="1 digit"):
            get_medium_level_occupation_codes(high_level_code)


# Low level codes

LOW_CODES = [
    {"code": "011", "preferredLabel": "Upseerit"},
    {"code": "021", "preferredLabel": "Aliupseerit"},
    {"code": "111", "preferredLabel": "Ylimmät virkamiehet ja järjestöjen johtajat"},
    {"code": "211", "preferredLabel": "Luonnon- ja geotieteen erityisasiantuntijat"},
]


@pytest.mark.parametrize("medium_level_code", ["01", "1", 1])
def test_low_level_armed_forces(medium_level_code):
    # A one-digit value gets a leading zero: 1 -> "01".
    with patch("mcp_server.main.read_file_contents_tojson", return_value=LOW_CODES):
        result = get_low_level_occupation_codes(medium_level_code)
    assert [item["code"] for item in result] == ["011"]


@pytest.mark.parametrize("medium_level_code", ["21", 21, " 21 "])
def test_low_level_normal_code(medium_level_code):
    # The medium level function accepts " 2 ", so the low level function must accept " 21 ".
    with patch("mcp_server.main.read_file_contents_tojson", return_value=LOW_CODES):
        result = get_low_level_occupation_codes(medium_level_code)
    assert [item["code"] for item in result] == ["211"]


def test_low_level_single_digit_is_normalised_not_rejected():
    # "5" means "05". No such group exists, so the result is empty.
    with patch("mcp_server.main.read_file_contents_tojson", return_value=LOW_CODES):
        assert get_low_level_occupation_codes("5") == []


@pytest.mark.parametrize("medium_level_code", ["021", "211", "abc", "", True, 2.0])
def test_low_level_rejects_code_that_is_not_two_digits(medium_level_code):
    # "021" was accepted before (int("021") == 21) but then matched nothing.
    with patch("mcp_server.main.read_file_contents_tojson", return_value=LOW_CODES):
        with pytest.raises(ValueError, match="2 digit"):
            get_low_level_occupation_codes(medium_level_code)


# Real data: the tools work as a chain, high -> medium -> low

@pytest.mark.parametrize("high_level_code", [str(n) for n in range(10)])
def test_real_data_chain_high_medium_low(high_level_code):
    # No mock. Every high level group has medium level groups, and every
    # medium level group that the tool returns has low level groups.
    medium = get_medium_level_occupation_codes(high_level_code)
    assert medium != []
    for item in medium:
        assert len(item["code"]) == 2 and item["code"].startswith(high_level_code)
        low = get_low_level_occupation_codes(item["code"])
        assert low != []
        assert all(len(i["code"]) == 3 and i["code"].startswith(item["code"]) for i in low)
