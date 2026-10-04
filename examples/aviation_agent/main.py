from typing import Any

import httpx

from maeyr.runtime import MaeyrAuth, mcp_endpoint

BASE_URL = "https://api.aviationstack.com/v1"
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class AviationStackError(RuntimeError):
    """A safe, nonempty error suitable for reporting a failed agent run."""


async def _get_flights(params: dict[str, Any]) -> list[dict[str, Any]]:
    api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            response = await client.get(
                f"{BASE_URL}/flights", params={**params, "access_key": api_key}
            )
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        raise AviationStackError("AviationStack request timed out. Please try again.") from None
    except httpx.HTTPStatusError:
        raise AviationStackError(
            "AviationStack returned an HTTP error. Please try again."
        ) from None
    except httpx.RequestError:
        raise AviationStackError("AviationStack could not be reached. Please try again.") from None
    except ValueError:
        raise AviationStackError("AviationStack returned an invalid response.") from None

    # Provider failures can use HTTP 200. Never expose their raw error text or
    # the request URL: both can contain the access key.
    if not isinstance(data, dict):
        raise AviationStackError("AviationStack returned an invalid response.")
    if "error" in data or data.get("success") is False:
        raise AviationStackError(
            "AviationStack rejected the request. Check your API plan and credentials."
        )
    flights = data.get("data")
    if not isinstance(flights, list) or any(not isinstance(flight, dict) for flight in flights):
        raise AviationStackError("AviationStack returned an invalid response.")
    return flights


@mcp_endpoint(description="Get flights between a given source and destination")
async def get_flights_between(payload: dict[str, Any]):
    flights = await _get_flights(
        {"dep_iata": payload.get("source"), "arr_iata": payload.get("destination")}
    )
    return {"flights": flights}


@mcp_endpoint(description="Get flight details by flight number")
async def get_flight_by_number(payload: dict[str, Any]):
    flights = await _get_flights({"flight_iata": payload.get("flight_number")})
    return {"flight_details": flights}


@mcp_endpoint(description="Get flights departing from a source at or after a specified time")
async def get_departures(payload: dict[str, Any]):
    time = payload.get("time")
    flights = await _get_flights({"dep_iata": payload.get("source"), "flight_status": "active"})
    filtered = [
        flight for flight in flights if flight.get("departure", {}).get("scheduled") >= time
    ]
    return {"departures": filtered}


@mcp_endpoint(description="Get flights arriving at a destination at or after a specified time")
async def get_arrivals(payload: dict[str, Any]):
    time = payload.get("time")
    flights = await _get_flights(
        {"arr_iata": payload.get("destination"), "flight_status": "active"}
    )
    filtered = [flight for flight in flights if flight.get("arrival", {}).get("scheduled") >= time]
    return {"arrivals": filtered}


@mcp_endpoint(description="Get all grounded flights or those with issues")
async def get_grounded_or_issues(payload: dict[str, Any]):
    flights = await _get_flights({})
    grounded = [
        flight
        for flight in flights
        if flight.get("flight_status") in ["cancelled", "diverted", "incident", "grounded"]
    ]
    return {"grounded_flights": grounded}
