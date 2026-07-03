"""MCP tools for log reading, error scanning and knowledge-base diagnosis."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from . import knowledge_base
from .config import Settings, get_settings
from .log_reader import LogAccessError, LogRecord, read_records

MAX_EXTRA_LINES = 20

_settings_override: Settings | None = None


def set_settings(settings: Settings | None) -> None:
    """Inject settings (used by tests); pass None to reset."""
    global _settings_override
    _settings_override = settings


def _settings() -> Settings:
    return _settings_override or get_settings()


def _record_dict(record: LogRecord) -> dict[str, Any]:
    extra = record.extra_lines[:MAX_EXTRA_LINES]
    result: dict[str, Any] = {
        "timestamp": record.timestamp.isoformat(),
        "level": record.level,
        "category": record.category,
        "message": record.message,
    }
    if extra:
        result["stackExcerpt"] = extra
        if len(record.extra_lines) > MAX_EXTRA_LINES:
            result["stackTruncated"] = len(record.extra_lines) - MAX_EXTRA_LINES
    return result


def _log_error(exc: LogAccessError) -> dict[str, Any]:
    return {"error": str(exc), "hint": exc.hint}


def _normalize_message(message: str) -> str:
    # Collapse numbers, UIDs and hex ids so repeats of the same error group together.
    normalized = re.sub(r"[0-9a-fA-F]{8,}|\d+(\.\d+)+|\d+", "#", message)
    return normalized[:80]


def _group_errors(records: list[LogRecord], max_groups: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = (record.category, _normalize_message(record.message))
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "level": record.level,
                "category": record.category,
                "count": 1,
                "firstSeen": record.timestamp.isoformat(),
                "lastSeen": record.timestamp.isoformat(),
                "sampleMessage": record.message,
                "sampleStackExcerpt": record.extra_lines[:MAX_EXTRA_LINES],
                "_sample_text": record.full_text,
            }
        else:
            group["count"] += 1
            group["lastSeen"] = record.timestamp.isoformat()
    ordered = sorted(groups.values(), key=lambda g: g["lastSeen"], reverse=True)
    return ordered[:max_groups]


GENERIC_TRIAGE_HINT = (
    "No knowledge-base entry matched. Generic triage: check that the db (PostgreSQL), "
    "ldap (slapd) and arc containers are all running; check storage space and "
    "permissions with get_storage_status; check FAILED tasks and their errorMessage "
    "with get_failed_tasks; read the surrounding log context with read_logs using a "
    "pattern filter."
)


async def read_logs(
    lines: int = 100,
    level: str | None = None,
    since_minutes: int | None = None,
    pattern: str | None = None,
) -> dict[str, Any]:
    """Read the tail of the DCM4CHEE (WildFly) server.log. Use this to inspect
    raw log context - e.g. around a failure time, or to follow what the archive
    is doing right now.

    lines: how many recent log lines to consider (capped server-side).
    level: minimum severity - one of TRACE, DEBUG, INFO, WARN, ERROR, FATAL
    ("WARN" returns WARN + ERROR + FATAL).
    since_minutes: only records from the last N minutes.
    pattern: case-insensitive regex matched against message and stack trace.
    Stack traces are attached to their log record (stackExcerpt).
    """
    since = (
        datetime.now() - timedelta(minutes=since_minutes) if since_minutes else None
    )
    try:
        records = read_records(
            _settings(), max_lines=lines, since=since, level=level, pattern=pattern
        )
    except LogAccessError as exc:
        return _log_error(exc)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"records": [_record_dict(r) for r in records], "count": len(records)}


async def get_recent_errors(
    since_minutes: int = 60, max_groups: int = 20
) -> dict[str, Any]:
    """Scan the server log for recent WARN/ERROR/FATAL entries and deduplicate
    them into error groups (same logger category + similar message). Use this
    to answer "did anything go wrong recently?" and as input for
    diagnose_error.

    Each group reports count, first/last occurrence, a sample message and a
    stack-trace excerpt. Groups are ordered most-recent first.
    """
    since = datetime.now() - timedelta(minutes=since_minutes)
    try:
        records = read_records(_settings(), since=since, level="WARN")
    except LogAccessError as exc:
        return _log_error(exc)
    groups = _group_errors(records, max_groups)
    for group in groups:
        group.pop("_sample_text", None)
    return {
        "sinceMinutes": since_minutes,
        "errorGroups": groups,
        "groupCount": len(groups),
        "totalRecords": len(records),
    }


async def diagnose_error(
    error_text: str | None = None, since_minutes: int = 60
) -> dict[str, Any]:
    """Explain WHY a DCM4CHEE error happened and HOW to fix it, using the
    built-in knowledge base of known DCM4CHEE 5.x failure signatures
    (association rejections, storage full/permissions, database/LDAP/Keycloak
    problems, HL7 and export failures, out-of-memory, ...).

    Two modes:
    - error_text given: diagnose that specific message/stack trace (e.g. a task
      errorMessage from get_failed_tasks, or a pasted log excerpt).
    - error_text omitted: automatically pull the recent error groups from the
      log (last since_minutes) and diagnose each.

    Returns matched causes and step-by-step resolutions; unmatched errors get a
    generic triage hint.
    """
    if error_text is not None:
        matches = knowledge_base.match(error_text)
        return {
            "input": error_text[:500],
            "diagnoses": [entry.to_dict() for entry in matches],
            "hint": None if matches else GENERIC_TRIAGE_HINT,
        }

    since = datetime.now() - timedelta(minutes=since_minutes)
    try:
        records = read_records(_settings(), since=since, level="WARN")
    except LogAccessError as exc:
        return _log_error(exc)
    groups = _group_errors(records, max_groups=20)
    results = []
    for group in groups:
        sample_text = group.pop("_sample_text", group["sampleMessage"])
        matches = knowledge_base.match(sample_text)
        results.append(
            {
                "error": {
                    "level": group["level"],
                    "category": group["category"],
                    "count": group["count"],
                    "lastSeen": group["lastSeen"],
                    "sampleMessage": group["sampleMessage"],
                },
                "diagnoses": [entry.to_dict() for entry in matches],
                "hint": None if matches else GENERIC_TRIAGE_HINT,
            }
        )
    return {
        "sinceMinutes": since_minutes,
        "diagnosedErrorGroups": results,
        "note": (
            "No WARN/ERROR entries found in the window." if not results else None
        ),
    }


def register_tools(mcp: Any) -> None:
    """Register all log/diagnosis tools on a FastMCP instance."""
    for fn in (read_logs, get_recent_errors, diagnose_error):
        mcp.tool()(fn)
