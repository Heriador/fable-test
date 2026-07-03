"""Tests for the WildFly log reader/parser."""

from datetime import datetime
from pathlib import Path

import pytest

from dcm4chee_mcp.config import Settings
from dcm4chee_mcp.log_reader import (
    LogAccessError,
    parse_log_lines,
    read_records,
    tail_file,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_server.log"


def fixture_lines() -> list[str]:
    return FIXTURE.read_text().splitlines()


def make_settings(**overrides) -> Settings:
    overrides.setdefault("log_file_path", FIXTURE)
    return Settings(_env_file=None, **overrides)


def test_parse_counts_and_fields():
    records = parse_log_lines(fixture_lines())
    assert len(records) == 10
    first = records[0]
    assert first.timestamp == datetime(2026, 7, 3, 8, 0, 1, 101000)
    assert first.level == "INFO"
    assert first.category == "org.jboss.as.server"
    assert first.thread == "ServerService Thread Pool -- 46"
    assert first.message.startswith("WFLYSRV0010")


def test_multiline_stack_traces_attach_to_record():
    records = parse_log_lines(fixture_lines())
    store_error = next(r for r in records if "Failed to store" in r.message)
    assert store_error.level == "ERROR"
    assert store_error.extra_lines[0] == "java.io.IOException: No space left on device"
    assert any("FileSystemStorage.write" in line for line in store_error.extra_lines)

    pool_error = next(r for r in records if "IJ000453" in r.message)
    assert any("Caused by" in line for line in pool_error.extra_lines)


def test_continuation_lines_before_first_header_are_skipped():
    lines = ["\tat some.cut.off.StackFrame(X.java:1)", *fixture_lines()]
    records = parse_log_lines(lines)
    assert len(records) == 10


def test_tail_file_returns_last_lines():
    all_lines = fixture_lines()
    assert tail_file(FIXTURE, 3) == all_lines[-3:]
    assert tail_file(FIXTURE, 10_000) == all_lines


def test_level_filter_includes_higher_severities():
    records = read_records(make_settings(), level="WARN")
    assert {r.level for r in records} == {"WARN", "ERROR"}
    assert len(records) == 4


def test_pattern_filter_matches_stack_trace_lines():
    records = read_records(make_settings(), pattern="no space left")
    assert len(records) == 1
    assert "Failed to store" in records[0].message


def test_since_filter():
    records = read_records(make_settings(), since=datetime(2026, 7, 3, 8, 21))
    assert len(records) == 3


def test_docker_fallback_used_when_file_missing():
    settings = make_settings(
        log_file_path=Path("/nonexistent/server.log"), docker_container="dcm4chee-arc"
    )
    calls = []

    def fake_docker_logs(container: str, tail: int) -> list[str]:
        calls.append((container, tail))
        return fixture_lines()

    records = read_records(settings, docker_logs=fake_docker_logs)
    assert calls == [("dcm4chee-arc", settings.max_log_lines)]
    assert len(records) == 10


def test_log_access_error_when_nothing_available():
    settings = make_settings(log_file_path=Path("/nonexistent/server.log"))
    with pytest.raises(LogAccessError) as exc_info:
        read_records(settings)
    assert "DCM4CHEE_DOCKER_CONTAINER" in exc_info.value.hint
    assert "/nonexistent/server.log" in str(exc_info.value)


def test_unknown_level_raises_value_error():
    with pytest.raises(ValueError):
        read_records(make_settings(), level="LOUD")
