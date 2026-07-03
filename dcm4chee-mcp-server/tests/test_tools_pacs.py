"""Tests for the PACS REST MCP tools (aggregation and error handling)."""

import re

import httpx
import pytest

from dcm4chee_mcp import tools_pacs
from dcm4chee_mcp.config import Settings
from dcm4chee_mcp.pacs_client import Dcm4cheeClient

BASE = "http://pacs.example:8080/dcm4chee-arc"


@pytest.fixture
async def injected_client():
    settings = Settings(_env_file=None, base_url=BASE)
    async with Dcm4cheeClient(settings) as client:
        tools_pacs.set_client(client)
        yield client
    tools_pacs.set_client(None)


async def test_get_server_status_degraded(injected_client, httpx_mock):
    httpx_mock.add_response(url=f"{BASE}/monitor/serverTime", json={"serverTime": "2026-07-03T10:00:00Z"})
    httpx_mock.add_response(url=f"{BASE}/queue/Export/count?status=FAILED", json={"count": 2})
    httpx_mock.add_response(
        url=re.compile(r".*/count\?status=(FAILED|SCHEDULED)$"),
        json={"count": 0},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=f"{BASE}/storage",
        json=[
            {
                "dcmStorageID": "fs1",
                "usableSpace": 1024**3,  # 1 GiB -> below the 5 GiB threshold
                "totalSpace": 100 * 1024**3,
                "status": {"MISSING_OBJECT": 3},
            }
        ],
    )
    httpx_mock.add_response(url=f"{BASE}/monitor/associations", json=[{"serialNo": 1}])

    result = await tools_pacs.get_server_status()

    assert result["status"] == "degraded"
    assert result["queues"]["Export"]["FAILED"] == 2
    assert any("queue Export has 2 FAILED" in p for p in result["problems"])
    assert any("MISSING_OBJECT=3" in p for p in result["problems"])
    assert any("below 5 GiB" in p for p in result["problems"])
    assert result["openAssociations"] == 1


async def test_get_server_status_unreachable(injected_client, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    result = await tools_pacs.get_server_status()
    assert result["status"] == "unreachable"
    assert "hint" in result


async def test_get_failed_tasks_aggregates_sources(injected_client, httpx_mock):
    failed_task = {
        "taskID": "42",
        "status": "FAILED",
        "failures": 3,
        "errorMessage": "java.net.UnknownHostException: remote-pacs",
        "irrelevantInternalField": "x",
    }
    httpx_mock.add_response(
        url=re.compile(r".*/queue/Export\?.*status=FAILED.*"), json=[failed_task]
    )
    httpx_mock.add_response(
        url=re.compile(r".*/(queue|monitor)/.*status=FAILED.*"), json=[], is_reusable=True
    )

    result = await tools_pacs.get_failed_tasks()

    assert result["totalFailedFound"] == 1
    assert list(result["queues"]) == ["Export"]
    task = result["queues"]["Export"][0]
    assert task["errorMessage"] == "java.net.UnknownHostException: remote-pacs"
    assert "irrelevantInternalField" not in task


async def test_echo_aet_translates_result_code(injected_client, httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/aets/DCM4CHEE/dimse/MODALITY1",
        method="POST",
        json={"result": 2, "errorMessage": "Connection refused"},
    )
    result = await tools_pacs.echo_aet("MODALITY1")
    assert result["meaning"] == "FailedToConnect"
    assert result["success"] is False


async def test_tool_returns_error_dict_on_404(injected_client, httpx_mock):
    httpx_mock.add_response(url=re.compile(r".*/queue/Bogus\?.*"), status_code=404, text="No such Queue")
    result = await tools_pacs.list_queue_tasks("Bogus")
    assert "error" in result
    assert "not found" in result["hint"].lower()
