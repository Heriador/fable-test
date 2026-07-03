"""Tests for the dcm4chee-arc REST client."""

import httpx
import pytest

from dcm4chee_mcp.config import Settings
from dcm4chee_mcp.pacs_client import Dcm4cheeClient, PacsError

BASE = "http://pacs.example:8080/dcm4chee-arc"


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, base_url=BASE, **overrides)


@pytest.fixture
async def client():
    async with Dcm4cheeClient(make_settings()) as c:
        yield c


async def test_get_server_time_url(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/monitor/serverTime", json={"serverTime": "2026-07-03T10:00:00Z"}
    )
    result = await client.get_server_time()
    assert result == {"serverTime": "2026-07-03T10:00:00Z"}
    assert httpx_mock.get_request().headers.get("Authorization") is None


async def test_auth_header_and_token_caching(httpx_mock):
    settings = make_settings(
        keycloak_token_url="https://kc.example:8843/realms/dcm4che/protocol/openid-connect/token",
        keycloak_client_id="mcp-client",
        keycloak_client_secret="s3cret",
    )
    httpx_mock.add_response(
        url=settings.keycloak_token_url,
        method="POST",
        json={"access_token": "tok-abc", "expires_in": 300},
    )
    httpx_mock.add_response(
        url=f"{BASE}/monitor/serverTime", json={"serverTime": "x"}, is_reusable=True
    )
    async with Dcm4cheeClient(settings) as client:
        await client.get_server_time()
        await client.get_server_time()

    token_requests = httpx_mock.get_requests(url=settings.keycloak_token_url)
    assert len(token_requests) == 1
    body = token_requests[0].content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=mcp-client" in body
    assert "client_secret=s3cret" in body

    api_requests = httpx_mock.get_requests(url=f"{BASE}/monitor/serverTime")
    assert len(api_requests) == 2
    for request in api_requests:
        assert request.headers["Authorization"] == "Bearer tok-abc"


async def test_list_queue_tasks_query_params(client, httpx_mock):
    httpx_mock.add_response(json=[{"taskID": "1"}])
    tasks = await client.list_queue_tasks(
        "Export", status="FAILED", limit=5, offset=10, created_time="-1d"
    )
    assert tasks == [{"taskID": "1"}]
    url = httpx_mock.get_request().url
    assert url.path.endswith("/queue/Export")
    assert url.params["status"] == "FAILED"
    assert url.params["limit"] == "5"
    assert url.params["offset"] == "10"
    assert url.params["createdTime"] == "-1d"
    assert url.params["orderby"] == "-updatedTime"


async def test_count_queue_tasks_omits_none_status(client, httpx_mock):
    httpx_mock.add_response(url=f"{BASE}/queue/Export/count", json={"count": "7"})
    assert await client.count_queue_tasks("Export") == 7
    assert "status" not in httpx_mock.get_request().url.params


async def test_echo_uses_dimse_path(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/aets/DCM4CHEE/dimse/MODALITY1?host=1.2.3.4&port=104",
        method="POST",
        json={"result": 0, "connectionTime": 5, "echoTime": 2, "releaseTime": 1},
    )
    result = await client.echo("MODALITY1", host="1.2.3.4", port=104)
    assert result["result"] == 0
    assert "/dimse/" in str(httpx_mock.get_request().url)


async def test_reschedule_returns_none_on_204(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/queue/Export/42/reschedule", method="POST", status_code=204
    )
    assert await client.reschedule_task("Export", 42) is None


async def test_non_2xx_raises_pacs_error(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/queue/NoSuchQueue/count", status_code=404, text="No such Queue"
    )
    with pytest.raises(PacsError) as exc_info:
        await client.count_queue_tasks("NoSuchQueue")
    err = exc_info.value
    assert err.status_code == 404
    assert "No such Queue" in str(err)
    assert f"{BASE}/queue/NoSuchQueue/count" in str(err)


async def test_connection_error_raises_pacs_error(client, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    with pytest.raises(PacsError) as exc_info:
        await client.get_server_time()
    assert "connection refused" in str(exc_info.value)
    assert exc_info.value.url == f"{BASE}/monitor/serverTime"


async def test_list_storage_usable_space_below(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/storage?usableSpaceBelow=5368709120",
        json=[{"dcmStorageID": "fs1"}],
    )
    storages = await client.list_storage(usable_space_below=5 * 1024**3)
    assert storages == [{"dcmStorageID": "fs1"}]
