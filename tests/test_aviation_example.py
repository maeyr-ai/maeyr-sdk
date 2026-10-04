"""Offline transport regressions for the AviationStack example agent."""

import traceback

import httpx
import pytest

from examples.aviation_agent import main as aviation

API_KEY = "dummy-offline-aviation-key"


@pytest.fixture
def mock_provider(monkeypatch):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        aviation.MaeyrAuth,
        "require_param",
        lambda method, param: API_KEY,
    )

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            aviation.httpx,
            "AsyncClient",
            lambda **kwargs: original_client(transport=transport, **kwargs),
        )

    return install


@pytest.mark.asyncio
async def test_flights_use_https_bounded_timeout_and_preserve_provider_records(mock_provider):
    flights = [
        {
            "flight_date": "2026-10-03",
            "flight_status": "active",
            "departure": {"iata": "MAA", "scheduled": "2026-10-03T10:00:00+00:00"},
            "arrival": {"iata": "DEL", "scheduled": "2026-10-03T13:00:00+00:00"},
            "flight": {"iata": "AI440"},
        }
    ]

    def handler(request):
        assert request.url.scheme == "https"
        assert request.url.host == "api.aviationstack.com"
        assert request.url.path == "/v1/flights"
        assert request.url.params["access_key"] == API_KEY
        assert request.url.params["dep_iata"] == "MAA"
        assert request.url.params["arr_iata"] == "DEL"
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 15.0,
            "write": 15.0,
            "pool": 15.0,
        }
        return httpx.Response(200, json={"pagination": {"total": 1}, "data": flights})

    mock_provider(handler)
    assert await aviation.get_flights_between({"source": "MAA", "destination": "DEL"}) == {
        "flights": flights
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "payload", "output_key"),
    [
        ("get_flights_between", {"source": "MAA", "destination": "DEL"}, "flights"),
        ("get_flight_by_number", {"flight_number": "AI440"}, "flight_details"),
        ("get_departures", {"source": "MAA", "time": "2026-10-03"}, "departures"),
        ("get_arrivals", {"destination": "DEL", "time": "2026-10-03"}, "arrivals"),
        ("get_grounded_or_issues", {}, "grounded_flights"),
    ],
)
async def test_genuine_empty_results_keep_each_endpoint_output_key(
    mock_provider, endpoint, payload, output_key
):
    mock_provider(lambda _request: httpx.Response(200, json={"data": []}))
    assert await getattr(aviation, endpoint)(payload) == {output_key: []}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["blank_timeout", "network", "http", "provider", "json"])
async def test_failures_raise_nonempty_errors_without_credential_or_provider_text(
    mock_provider, failure
):
    raw_message = f"sensitive-provider-message access_key={API_KEY}"

    def handler(request):
        if failure == "blank_timeout":
            raise httpx.ConnectTimeout("", request=request)
        if failure == "network":
            raise httpx.ConnectError(raw_message, request=request)
        if failure == "http":
            return httpx.Response(401, text=raw_message)
        if failure == "provider":
            return httpx.Response(200, json={"error": {"code": 101, "info": raw_message}})
        return httpx.Response(200, text=raw_message)

    mock_provider(handler)
    with pytest.raises(aviation.AviationStackError) as error:
        await aviation.get_flights_between({"source": "MAA", "destination": "DEL"})
    rendered = "".join(traceback.format_exception(error.value))
    assert str(error.value).strip()
    assert API_KEY not in rendered
    assert "access_key" not in rendered
    assert "api.aviationstack.com" not in rendered
    assert "sensitive-provider-message" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], {}, {"data": None}, {"data": ["invalid"]}])
async def test_invalid_provider_shapes_fail_instead_of_looking_empty(mock_provider, body):
    mock_provider(lambda _request: httpx.Response(200, json=body))
    with pytest.raises(aviation.AviationStackError, match="invalid response"):
        await aviation.get_flights_between({"source": "MAA", "destination": "DEL"})


@pytest.mark.asyncio
async def test_redirect_is_not_followed_with_the_access_key(mock_provider):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://untrusted.test/flights"})

    mock_provider(handler)
    with pytest.raises(aviation.AviationStackError, match="HTTP error"):
        await aviation.get_flights_between({"source": "MAA", "destination": "DEL"})
    assert len(requests) == 1
    assert requests[0].url.scheme == "https"
