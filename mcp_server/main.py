from httpx import AsyncClient, HTTPStatusError, RequestError, TimeoutException
from json import load as jload
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.logging import get_logger
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List
import logging

# CONSTANTS
API_URL = "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v1/search"
API_TIMEOUT_SECONDS = 5.0
# Max characters of the API response body to include in an error message
ERROR_BODY_MAX_CHARS = 500

# PATHS
OCCUPATIONS_HIGH_LEVEL_FILE = Path(__file__).resolve().parent.parent / "data" / "ammatit_taso_1.json"
OCCUPATIONS_MEDIUM_LEVEL_FILE = Path(__file__).resolve().parent.parent / "data" / "ammatit_taso_2.json"
OCCUPATIONS_LOW_LEVEL_FILE = Path(__file__).resolve().parent.parent / "data" / "ammatit_taso_3.json"
OCCUPATIONS_LEVEL4_FILE = Path(__file__).resolve().parent.parent / "data" / "ammatit_taso_4.json"
MUNICIPALITIES_FILE= Path(__file__).resolve().parent.parent / "data" / "kunnat.json"
#LOG_PATH = Path(__file__).resolve().parent / "tyomarkkinatori_mcp.log"

# Server side logging
logging.basicConfig(level=logging.INFO, force=True, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = get_logger(__name__)

mcp = FastMCP(name="tyomarkkinatori")

# Assert necessary files exists
def assertFilesAndStructure(fileLocation: Path, keys: list = ["code", "preferredLabel"]):
    with fileLocation.open("r", encoding="utf-8") as f:
        key_values = jload(f)[0].keys()
        assert set(keys).issubset(key_values), f"{fileLocation} missing required keys"
for file, keys in [
    (OCCUPATIONS_HIGH_LEVEL_FILE, ["code", "preferredLabel"]),
    (OCCUPATIONS_MEDIUM_LEVEL_FILE, ["code", "preferredLabel"]),
    (OCCUPATIONS_LOW_LEVEL_FILE, ["code", "preferredLabel"]),
    (OCCUPATIONS_LEVEL4_FILE, ["code", "preferredLabel"]),
    (MUNICIPALITIES_FILE, ["code", "classificationName"])]:
    try:
        assertFilesAndStructure(fileLocation=file, keys=keys)
    except AssertionError:
        logger.error(f"Assertion error in resource file: {file}")
        raise AssertionError


def read_file_contents_tojson(filelocation: Path) -> list:
    """
    Reads file contents from a file and returns it with json.load()
    Args:
        filelocation (Path): Path to the JSON resource
    Returns:
        list: The JSON content loaded from the file. Assumes the top-level JSON structure is a list.

    """
    logger.info(f'reading {filelocation}')
    with open(file=filelocation, mode="r", encoding="utf-8") as f:
        return jload(f)


def normalise_occupation_code(value: int|str, digits: int) -> str:
    """
    Returns an ISCO-08 occupation code as a string with exactly `digits` digits.
    The codes for armed forces start with "0" ("0", "01", "011", "0110"), so a code
    is a string, not a number. A shorter value gets leading zeros: 1 -> "01" when digits=2.
    Args:
        value (int | str): the code from the tool input.
        digits (int): the number of digits that a code on this level has.
    Raises:
        ValueError: if the value is not 1 to `digits` digits.
    Returns:
        str: the code with leading zeros.
    """
    code = str(value).strip()
    if not (code.isascii() and code.isdigit()) or len(code) > digits:
        raise ValueError(
            f"Invalid occupation code {value!r}: expected a code with {digits} digit(s), "
            f"for example {'1'.zfill(digits)!r}. Give the code as a string to keep a leading zero."
        )
    return code.zfill(digits)


@mcp.tool()
def get_location_codes(first_letters_of_the_city: str) -> list:
    """Get location codes for the api call. Use text to narrow down the search (and to waste less context)
    Only Finnish city names are included.
    Args:
        first_letters_of_the_city (str): "Helsinki" or "Vaasa" or "Hel" or "Tur"... 
    Returns:
        list: returns a list of the results. The list can be empty or contain JSON objects. 
    """
    def normalise(text : str):
        return text.lower()
    
    # In case of an empty input 
    if len(first_letters_of_the_city) < 1:
        logger.warning(f'get_location_codes: empty input')
        return []
    
    json_contents = read_file_contents_tojson(filelocation=MUNICIPALITIES_FILE)
    json_parsed = []
    for i in json_contents:
        if normalise(i["classificationName"][:len(first_letters_of_the_city)]) == normalise(first_letters_of_the_city):
            # Include cities that matches the criteria ("turku"[:3] == "tur") == True
            json_parsed.append(i)
        
    return json_parsed
    
    
@mcp.tool()
def get_all_high_level_occupation_codes() -> list:
    """Returns all high level occupation codes. 
    Can be used for the API call or for getting medium level occupation codes. 

    Returns:
        list: list that contains all high level occupation categories and the related codes. 
    """
    return read_file_contents_tojson(OCCUPATIONS_HIGH_LEVEL_FILE)


@mcp.tool()
def get_medium_level_occupation_codes(high_level_code: int|str) -> list:
    """
    Returns medium level occupational category codes for the API call. 
    Generally using these codes will result in medium amount of results. 
    Uses high level code as a base criteria for the search.
    Args:
        high_level_code (int | str): one digit, "0" - "9". "0" is armed forces.
    Raises:
        ValueError: Raises value error if the code is not one digit.
        e: Except never to be excepted
    Returns:
        list: If the function does not raise an error, it will return a list.
        Each code has two digits and starts with high_level_code, for example "01" or "21".
        The list will have items as long as the input list is not altered.
    """

    try:
        code = normalise_occupation_code(high_level_code, digits=1)

    except Exception as e:
        logger.error(f"Something went wrong in get_medium_level_occupation_codes. {e}")
        raise e

    codes = read_file_contents_tojson(OCCUPATIONS_MEDIUM_LEVEL_FILE)
    return [i for i in codes if i["code"].startswith(code)]


@mcp.tool()
def get_low_level_occupation_codes(medium_level_code: int|str) -> list:
    
    """
    Returns low level occupational category codes for the API call. 
    Generally using these codes will result in lower amount of results from API call.
    Uses medium level code as a base criteria for the search.
    Args:
        medium_level_code (int | str): two digits, for example "01" or "21".
        Give it as a string to keep the leading zero. One digit gets a leading zero ("5" -> "05").
    Raises:
        ValueError: Raises value error if the code has more than two digits or is not a number.
        e: Except never to be excepted
    Returns:
        list: If the function does not raise an error, it will return a list.
        Each code has three digits and starts with the medium level code, for example "011" or "211".
        The list might be empty.
    """

    try:
        code = normalise_occupation_code(medium_level_code, digits=2)

    except Exception as e:
        logger.error(f"Something went wrong in get_medium_level_occupation_codes. {e}")
        raise e


    codes = read_file_contents_tojson(OCCUPATIONS_LOW_LEVEL_FILE)
    return [i for i in codes if i["code"].startswith(code)]


class TyomarkkinatoriAPIError(ToolError):
    """
    Base class for errors from the Työmarkkinatori API call.
    FastMCP sends the message of a raised exception to the MCP client
    as a tool result with isError=True, so the message must tell the
    client (LLM) what failed and what it can do.
    """


class APITimeoutError(TyomarkkinatoriAPIError):
    """The API did not respond in time."""


class APIHTTPStatusError(TyomarkkinatoriAPIError):
    """The API responded with an HTTP error status (4xx or 5xx)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class APINetworkError(TyomarkkinatoriAPIError):
    """The request did not reach the API (DNS, connection, TLS...)."""


class APIInvalidResponseError(TyomarkkinatoriAPIError):
    """The API responded with a body that is not valid JSON."""


def _truncate(text: str, max_chars: int = ERROR_BODY_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) > max_chars:
        return text[:max_chars] + "... (truncated)"
    return text


class API_call(BaseModel):
    municipalities: list[int|str] = Field(default=[], min_length=0, max_length=10, description="list of the municipality numbers.")
    occupations: list[int|str] = Field(default=[], min_length=0, max_length=10, description="list of the occupation codes (ISCO-08, 1-4 digits) as strings. Keep the leading zero, for example \"021\".")


@mcp.tool()
async def api_call(input: API_call):
    """
    Make an API call to the Työmarkkinatori API to receive job listings. 

    Args:
        input (API_call): API_call model containing two lists:
        - municipalities
        - occupations
            
    Notes:
    - Both fields default to empty lists.

    Raises:
        APITimeoutError: The API did not respond in time. Retrying later can help.
        APIHTTPStatusError: The API returned an HTTP error status.
            4xx usually means the request (codes) is not valid; 5xx means a server-side problem.
        APINetworkError: The API could not be reached.
        APIInvalidResponseError: The API response is not valid JSON.
    """
    
    def create_request(mun: List[str], isco: List[str]):
        def add_leading_zeros(value: str) -> str:
            return value.zfill(3)

        data = {
            "query": "",
            "paging": {"pageSize": 30, "pageNumber": 0},
            "filters": {
                "publishedAfter": "",
                "closesBefore": "",
                "municipalities": [add_leading_zeros(m) for m in mun],
                "iscoNotations": isco,
                "regions": [],
                "countries": [],
            },
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        return headers, data

    municipalities_list = [str(m) for m in input.municipalities]
    occupations_list = [str(o) for o in input.occupations]

    headers, data = create_request(
        mun=municipalities_list,
        isco=occupations_list
    )

    async with AsyncClient() as client:
        try:
            logger.info('Sending request to API.')
            response = await client.post(
                url=API_URL,
                headers=headers,
                json=data,
                timeout=API_TIMEOUT_SECONDS
            )
            logger.info(f'Response {response.status_code}')
            response.raise_for_status()

        # TimeoutException is a subclass of RequestError, so it must come first.
        except TimeoutException as e:
            logger.exception(f"API call timed out: {e}")
            raise APITimeoutError(
                f"Työmarkkinatori API timeout: no response within {API_TIMEOUT_SECONDS} seconds "
                f"({type(e).__name__}). The service can be slow or unavailable; try again later."
            ) from e

        except HTTPStatusError as e:
            status = e.response.status_code
            body = _truncate(e.response.text)
            logger.exception(f"API call returned HTTP {status}: {body}")
            if 400 <= status < 500:
                hint = ("The API did not accept the request. Check that the municipality codes "
                        "and occupation codes are valid (use the code lookup tools).")
            else:
                hint = "The Työmarkkinatori service has a problem; try again later."
            message = f"Työmarkkinatori API returned HTTP {status} {e.response.reason_phrase}. {hint}"
            if body:
                message += f" Response body: {body}"
            raise APIHTTPStatusError(message, status_code=status) from e

        except RequestError as e:
            logger.exception(f"API call network error: {e}")
            raise APINetworkError(
                f"Työmarkkinatori API network error: could not reach {API_URL} "
                f"({type(e).__name__}: {e}). Check the network connection and try again."
            ) from e

    try:
        return response.json()
    except ValueError as e:
        # json.JSONDecodeError is a subclass of ValueError
        body = _truncate(response.text)
        logger.exception(f"API response is not valid JSON: {body}")
        raise APIInvalidResponseError(
            f"Työmarkkinatori API returned HTTP {response.status_code} with a response that is "
            f"not valid JSON. Response body: {body}"
        ) from e


def main():
    logger.info("Server starting...")
    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.exception(f"MCP server crashed. {e}")
    finally:
        logger.info("Server shutting down...")


if __name__ == "__main__":
    main()
