"""Read and parse WildFly server.log files from a DCM4CHEE Archive deployment.

Supports tailing the (typically volume-mounted) server.log efficiently and,
as a fallback, reading console output via ``docker logs``.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

# WildFly default pattern: %d{yyyy-MM-dd HH:mm:ss,SSS} %-5p [%c] (%t) %s%e%n
# The console handler omits the date part, so the date group is optional.
_HEADER_RE = re.compile(
    r"^(?:(?P<date>\d{4}-\d{2}-\d{2})\s)?"
    r"(?P<time>\d{2}:\d{2}:\d{2},\d{3})\s+"
    r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
    r"\[(?P<category>[^\]]*)\]\s+"
    r"\((?P<thread>[^)]*)\)\s?"
    r"(?P<message>.*)$"
)

_LEVEL_SEVERITY = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}


class LogAccessError(Exception):
    """Raised when the server log can be reached neither on disk nor via docker."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint or ""


@dataclass
class LogRecord:
    timestamp: datetime
    level: str
    category: str
    thread: str
    message: str
    extra_lines: list[str] = field(default_factory=list)

    @property
    def raw(self) -> str:
        header = (
            f"{self.timestamp.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} "
            f"{self.level:<5} [{self.category}] ({self.thread}) {self.message}"
        )
        return "\n".join([header, *self.extra_lines])

    @property
    def full_text(self) -> str:
        return "\n".join([self.message, *self.extra_lines])


def parse_log_lines(lines: Iterable[str]) -> list[LogRecord]:
    """Parse raw log lines into records.

    Lines that do not match the header pattern (stack traces, ``Caused by:``,
    wrapped messages) are attached to the preceding record's ``extra_lines``.
    Non-matching lines before the first header (e.g. a stack trace cut off by
    tailing) are skipped.
    """
    records: list[LogRecord] = []
    for line in lines:
        line = line.rstrip("\r\n")
        m = _HEADER_RE.match(line)
        if m:
            date = m.group("date") or datetime.now().strftime("%Y-%m-%d")
            timestamp = datetime.strptime(f"{date} {m.group('time')}", "%Y-%m-%d %H:%M:%S,%f")
            records.append(
                LogRecord(
                    timestamp=timestamp,
                    level=m.group("level"),
                    category=m.group("category"),
                    thread=m.group("thread"),
                    message=m.group("message"),
                )
            )
        elif records:
            records[-1].extra_lines.append(line)
        # else: continuation line before any header -> skip
    return records


def tail_file(path: Path | str, max_lines: int) -> list[str]:
    """Return up to the last ``max_lines`` lines of ``path``.

    Reads fixed-size blocks backwards from the end of the file so that
    multi-gigabyte logs are never loaded into memory.
    """
    if max_lines <= 0:
        return []
    block_size = 64 * 1024
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= max_lines:
            step = min(block_size, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.splitlines()
    return [line.decode("utf-8", errors="replace") for line in lines[-max_lines:]]


def _run_docker_logs(container: str, tail: int) -> list[str]:
    """Fetch the last ``tail`` console lines of a container via ``docker logs``."""
    proc = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"docker logs exited with {proc.returncode}: {detail}")
    output = proc.stdout
    # docker logs replays the container's stderr stream on stderr
    if proc.stderr:
        output = output + ("\n" if output and not output.endswith("\n") else "") + proc.stderr
    return output.splitlines()[-tail:]


def read_records(
    settings,
    max_lines: int | None = None,
    since: datetime | None = None,
    level: str | None = None,
    pattern: str | None = None,
    docker_logs: Callable[[str, int], list[str]] | None = None,
) -> list[LogRecord]:
    """Tail, parse and filter the DCM4CHEE server log.

    Reads from ``settings.log_file_path``; when the file is missing or
    unreadable and ``settings.docker_container`` is set, falls back to
    ``docker logs``. ``docker_logs`` allows injecting the subprocess call
    for tests.

    Filters:
      since   -- keep records with ``timestamp >= since``
      level   -- minimum severity (``"WARN"`` keeps WARN, ERROR and FATAL)
      pattern -- case-insensitive regex matched against message + extra lines
    """
    limit = settings.max_log_lines if max_lines is None else min(max_lines, settings.max_log_lines)
    attempts: list[str] = []
    lines: list[str] | None = None
    try:
        lines = tail_file(settings.log_file_path, limit)
    except OSError as exc:
        attempts.append(f"read {settings.log_file_path}: {exc}")
        if settings.docker_container:
            runner = docker_logs or _run_docker_logs
            try:
                lines = runner(settings.docker_container, limit)
            except Exception as exc2:
                attempts.append(f"docker logs {settings.docker_container}: {exc2}")
        else:
            attempts.append("docker fallback skipped: DCM4CHEE_DOCKER_CONTAINER is not set")

    if lines is None:
        hint = (
            "Mount the WildFly log volume so this process can read "
            "/opt/wildfly/standalone/log/server.log (set DCM4CHEE_LOG_FILE_PATH to the "
            "host path), or set DCM4CHEE_DOCKER_CONTAINER to the dcm4chee-arc container "
            "name so 'docker logs' can be used as a fallback."
        )
        raise LogAccessError(
            "Could not access DCM4CHEE server logs. Tried: " + "; ".join(attempts),
            hint=hint,
        )

    records = parse_log_lines(lines)

    if since is not None:
        records = [r for r in records if r.timestamp >= since]
    if level is not None:
        threshold = _LEVEL_SEVERITY.get(level.upper())
        if threshold is None:
            raise ValueError(
                f"Unknown log level {level!r}; expected one of {sorted(_LEVEL_SEVERITY)}"
            )
        records = [r for r in records if _LEVEL_SEVERITY.get(r.level, 0) >= threshold]
    if pattern is not None:
        rx = re.compile(pattern, re.IGNORECASE)
        records = [r for r in records if rx.search(r.full_text)]
    return records
