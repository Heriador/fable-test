"""Tests for the DCM4CHEE error knowledge base and diagnosis tools."""

from pathlib import Path

from dcm4chee_mcp import knowledge_base, tools_logs
from dcm4chee_mcp.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "sample_server.log"


def test_all_entries_load_and_regexes_compile():
    entries = knowledge_base.load_entries()
    assert len(entries) >= 14
    for entry in entries:
        assert entry.id and entry.title and entry.cause
        assert entry.signatures
        assert entry.resolution
        assert entry.severity in ("critical", "warning", "info")


def test_match_db_connection_pool():
    text = (
        "IJ000453: Unable to get managed connection for java:/PacsDS: "
        "IJ000655: No managed connections available within configured blocking timeout"
    )
    matches = knowledge_base.match(text)
    assert matches
    assert matches[0].id == "db-connection-pool"


def test_match_association_rejected():
    matches = knowledge_base.match(
        "Association rejected: result=2, source=1, reason=3 - "
        "calling-AE-title-not-recognized: UNKNOWN_AET"
    )
    assert [m.id for m in matches][0] == "association-rejected-calling-aet"


def test_match_returns_empty_for_unknown_text():
    assert knowledge_base.match("everything is perfectly fine here") == []


async def test_diagnose_error_with_text():
    result = await tools_logs.diagnose_error(error_text="java.io.IOException: No space left on device")
    assert result["diagnoses"]
    assert result["diagnoses"][0]["id"] == "storage-out-of-space"
    assert result["hint"] is None


async def test_diagnose_error_unmatched_gets_triage_hint():
    result = await tools_logs.diagnose_error(error_text="some brand new mystery failure")
    assert result["diagnoses"] == []
    assert "triage" in result["hint"].lower() or "get_failed_tasks" in result["hint"]


async def test_diagnose_error_from_recent_logs():
    tools_logs.set_settings(Settings(_env_file=None, log_file_path=FIXTURE))
    try:
        result = await tools_logs.diagnose_error(since_minutes=10_000_000)
        groups = result["diagnosedErrorGroups"]
        assert groups
        diagnosed_ids = {
            d["id"] for group in groups for d in group["diagnoses"]
        }
        assert "db-connection-pool" in diagnosed_ids
        assert "storage-out-of-space" in diagnosed_ids
        assert "association-rejected-calling-aet" in diagnosed_ids
    finally:
        tools_logs.set_settings(None)


async def test_get_recent_errors_groups(tmp_path):
    tools_logs.set_settings(Settings(_env_file=None, log_file_path=FIXTURE))
    try:
        result = await tools_logs.get_recent_errors(since_minutes=10_000_000)
        assert result["groupCount"] >= 3
        levels = {g["level"] for g in result["errorGroups"]}
        assert levels <= {"WARN", "ERROR", "FATAL"}
        pool_group = next(
            g for g in result["errorGroups"] if "IJ000453" in g["sampleMessage"]
        )
        assert pool_group["sampleStackExcerpt"]
    finally:
        tools_logs.set_settings(None)


async def test_read_logs_error_dict_when_inaccessible(tmp_path):
    tools_logs.set_settings(
        Settings(_env_file=None, log_file_path=tmp_path / "missing.log")
    )
    try:
        result = await tools_logs.read_logs()
        assert "error" in result and "hint" in result
    finally:
        tools_logs.set_settings(None)
