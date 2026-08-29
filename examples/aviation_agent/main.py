from typing import Any, Dict

import httpx

from maeyr.runtime import MaeyrAuth, mcp_endpoint

BASE_URL = "http://api.aviationstack.com/v1"


@mcp_endpoint(description="Get flights between a given source and destination")
async def get_flights_between(payload: Dict[str, Any]):
    source = payload.get("source")
    destination = payload.get("destination")
    api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/flights",
                params={
                    "access_key": api_key,
                    "dep_iata": source,
                    "arr_iata": destination,
                },
            )
            data = response.json()
            return {"flights": data.get("data", [])}
        except Exception as e:
            return {"flights": [], "error": str(e)}


@mcp_endpoint(description="Get flight details by flight number")
async def get_flight_by_number(payload: Dict[str, Any]):
    flight_number = payload.get("flight_number")
    api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/flights",
                params={"access_key": api_key, "flight_iata": flight_number},
            )
            data = response.json()
            return {"flight_details": data.get("data", [])}
        except Exception as e:
            return {"flight_details": [], "error": str(e)}


@mcp_endpoint(description="Get flights departing from a source at or after a specified time")
async def get_departures(payload: Dict[str, Any]):
    source = payload.get("source")
    time = payload.get("time")
    api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/flights",
                params={
                    "access_key": api_key,
                    "dep_iata": source,
                    "flight_status": "active",
                },
            )
            data = response.json()
            filtered = [
                f
                for f in data.get("data", [])
                if f.get("departure", {}).get("scheduled") >= time
            ]
            return {"departures": filtered}
        except Exception as e:
            return {"departures": [], "error": str(e)}


@mcp_endpoint(description="Get flights arriving at a destination at or after a specified time")
async def get_arrivals(payload: Dict[str, Any]):
    destination = payload.get("destination")
    time = payload.get("time")
    api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/flights",
                params={
                    "access_key": api_key,
                    "arr_iata": destination,
                    "flight_status": "active",
                },
            )
            data = response.json()
            filtered = [
                f
                for f in data.get("data", [])
                if f.get("arrival", {}).get("scheduled") >= time
            ]
            return {"arrivals": filtered}
        except Exception as e:
            return {"arrivals": [], "error": str(e)}


@mcp_endpoint(description="Get all grounded flights or those with issues")
async def get_grounded_or_issues(payload: Dict[str, Any]):
    api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/flights",
                params={"access_key": api_key},
            )
            data = response.json()
            grounded = [
                f
                for f in data.get("data", [])
                if f.get("flight_status")
                in ["cancelled", "diverted", "incident", "grounded"]
            ]
            return {"grounded_flights": grounded}
        except Exception as e:
            return {"grounded_flights": [], "error": str(e)}
