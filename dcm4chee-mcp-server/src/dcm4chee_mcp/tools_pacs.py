"""MCP tools exposing the dcm4chee-arc REST monitoring API."""

from __future__ import annotations

import logging
from typing import Any

from .config import get_settings
from .pacs_client import (
    DEFAULT_QUEUES,
    ECHO_RESULT_MEANINGS,
    MONITOR_FAMILIES,
    TASK_STATUSES,
    Dcm4cheeClient,
    PacsError,
)

logger = logging.getLogger(__name__)

GIB = 1024**3
LOW_SPACE_THRESHOLD_BYTES = 5 * GIB

# Storage status counters that indicate a real problem when > 0.
WORRYING_STORAGE_KEYS = (
    "FAILED_TO_DELETE",
    "MISSING_OBJECT",
    "FAILED_TO_FETCH_OBJECT",
    "DIFFERING_OBJECT_CHECKSUM",
    "ORPHANED",
)

# Fields kept when trimming task objects for the model context.
TASK_BASE_FIELDS = (
    "taskID",
    "queue",
    "type",
    "status",
    "failures",
    "createdTime",
    "updatedTime",
    "scheduledTime",
    "processingStartTime",
    "processingEndTime",
    "errorMessage",
    "outcomeMessage",
    "batchID",
)
TASK_EXTRA_FIELDS = (
    "dicomDeviceName",
    "LocalAET",
    "RemoteAET",
    "DestinationAET",
    "ExporterID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "QueryRetrieveLevel",
    "StorageID",
)

_client: Dcm4cheeClient | None = None


def set_client(client: Dcm4cheeClient | None) -> None:
    """Inject a preconfigured client (used by tests); pass None to reset."""
    global _client
    _client = client


def _get_client() -> Dcm4cheeClient:
    global _client
    if _client is None:
        _client = Dcm4cheeClient(get_settings())
    return _client


def _error_dict(exc: PacsError) -> dict[str, Any]:
    if exc.status_code in (401, 403):
        hint = (
            "Authentication/authorization failed. Check DCM4CHEE_KEYCLOAK_TOKEN_URL, "
            "DCM4CHEE_KEYCLOAK_CLIENT_ID and DCM4CHEE_KEYCLOAK_CLIENT_SECRET, and that "
            "the client has the required realm roles."
        )
    elif exc.status_code == 404:
        hint = (
            "The resource was not found. Check the queue/AE/task identifier, and that "
            "DCM4CHEE_BASE_URL points at the archive root (e.g. "
            "http://host:8080/dcm4chee-arc)."
        )
    elif exc.status_code is not None:
        hint = "The archive rejected the request; the response text above usually explains why."
    else:
        hint = (
            "Could not reach the archive REST API. Check DCM4CHEE_BASE_URL, that the "
            "dcm4chee-arc container is running, and network connectivity."
        )
    return {"error": str(exc), "hint": hint}


def _trim_task(task: dict[str, Any]) -> dict[str, Any]:
    trimmed = {k: task[k] for k in TASK_BASE_FIELDS if k in task}
    trimmed.update({k: task[k] for k in TASK_EXTRA_FIELDS if k in task})
    return trimmed


def _human_size(num_bytes: Any) -> str | None:
    if num_bytes is None:
        return None
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(size) < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return None


def _storage_problems(storage: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    usable = storage.get("usableSpace")
    if usable is not None and int(usable) < LOW_SPACE_THRESHOLD_BYTES:
        problems.append(f"usable space below 5 GiB ({_human_size(usable)})")
    status = storage.get("status") or {}
    for key in WORRYING_STORAGE_KEYS:
        count = status.get(key)
        if count:
            problems.append(f"{key}={count}")
    return problems


async def get_server_status() -> dict[str, Any]:
    """Overall health check of the DCM4CHEE archive. Use this FIRST when asked
    "is the PACS ok?", "what is the status?", or before deeper diagnosis.

    Combines in one call: REST reachability (server time), FAILED/SCHEDULED task
    counts per default queue, FAILED counts per monitor task family
    (export/retrieve/stgver/diff), a storage summary (free space and problem
    counters per storage system), and the number of open DICOM associations.

    Returns a dict with "status": "healthy" | "degraded" | "unreachable" and a
    "problems" list explaining any degradation. Follow up with get_failed_tasks,
    get_storage_status or list_queue_tasks for details.
    """
    client = _get_client()
    try:
        server_time = await client.get_server_time()
    except PacsError as exc:
        return {"status": "unreachable", **_error_dict(exc)}

    problems: list[str] = []
    queues: dict[str, Any] = {}
    for queue in DEFAULT_QUEUES:
        try:
            failed = await client.count_queue_tasks(queue, status="FAILED")
            scheduled = await client.count_queue_tasks(queue, status="SCHEDULED")
        except PacsError as exc:
            if exc.status_code == 404:
                continue  # queue not configured on this archive
            queues[queue] = {"error": str(exc)}
            continue
        queues[queue] = {"FAILED": failed, "SCHEDULED": scheduled}
        if failed > 0:
            problems.append(f"queue {queue} has {failed} FAILED task(s)")

    monitor: dict[str, Any] = {}
    for family in MONITOR_FAMILIES:
        try:
            failed = await client.count_monitor_tasks(family, status="FAILED")
        except PacsError as exc:
            if exc.status_code == 404:
                continue
            monitor[family] = {"error": str(exc)}
            continue
        monitor[family] = {"FAILED": failed}
        if failed > 0:
            problems.append(f"monitor family {family} has {failed} FAILED task(s)")

    storage_summary: list[dict[str, Any]] = []
    try:
        for storage in await client.list_storage():
            storage_problems = _storage_problems(storage)
            storage_summary.append(
                {
                    "storageID": storage.get("dcmStorageID"),
                    "usableSpace": _human_size(storage.get("usableSpace")),
                    "totalSpace": _human_size(storage.get("totalSpace")),
                    "readOnly": storage.get("dcmReadOnly", False),
                    "problems": storage_problems,
                }
            )
            for problem in storage_problems:
                problems.append(f"storage {storage.get('dcmStorageID')}: {problem}")
    except PacsError as exc:
        storage_summary = [{"error": str(exc)}]

    open_associations: Any
    try:
        open_associations = len(await client.list_associations())
    except PacsError as exc:
        open_associations = {"error": str(exc)}

    return {
        "status": "degraded" if problems else "healthy",
        "serverTime": server_time.get("serverTime"),
        "problems": problems,
        "queues": queues,
        "monitorTasks": monitor,
        "storage": storage_summary,
        "openAssociations": open_associations,
    }


async def list_aets() -> dict[str, Any]:
    """List the archive's own AE titles and all configured AEs (archive and
    remote modalities/PACS). Use this to discover valid AE titles before
    echo_aet, or when asked which devices/modalities are configured.
    """
    client = _get_client()
    try:
        archive_aets = await client.list_aets()
        aes = await client.list_aes()
    except PacsError as exc:
        return _error_dict(exc)
    return {
        "archiveAETitles": [
            ae.get("dicomAETitle") if isinstance(ae, dict) else ae for ae in archive_aets
        ],
        "configuredAEs": [
            {
                "dicomAETitle": ae.get("dicomAETitle"),
                "dicomDescription": ae.get("dicomDescription"),
                "deviceName": ae.get("dicomDeviceName"),
            }
            for ae in aes
        ],
    }


async def echo_aet(
    remote_aet: str, host: str | None = None, port: int | None = None
) -> dict[str, Any]:
    """Send a DICOM C-ECHO to a remote AE via the archive's REST API to verify
    DICOM connectivity. Use this when a modality/PACS is not receiving or
    sending images, or after network/configuration changes.

    remote_aet must be a configured AE title (see list_aets), unless host and
    port are given to override the configured connection. Returns the numeric
    result code plus a human-readable "meaning" (e.g. Success, FailedToConnect,
    AssociationRejected) and connect/echo/release timings in ms.
    """
    client = _get_client()
    try:
        result = await client.echo(remote_aet, host=host, port=port)
    except PacsError as exc:
        return _error_dict(exc)
    result = dict(result or {})
    code = result.get("result")
    if code is not None:
        result["meaning"] = ECHO_RESULT_MEANINGS.get(int(code), f"Unknown result code {code}")
    result["success"] = code == 0
    return result


async def list_queues() -> dict[str, Any]:
    """List all task queues of the archive with their descriptions and the
    number of tasks per status (SCHEDULED, IN PROCESS, COMPLETED, WARNING,
    FAILED, CANCELED). Use this to get an overview of background processing
    (export, retrieve, HL7, storage commitment, ...) before drilling into a
    specific queue with list_queue_tasks.
    """
    client = _get_client()
    try:
        descriptors = await client.list_queues()
    except PacsError as exc:
        return _error_dict(exc)
    result = []
    for descriptor in descriptors:
        name = descriptor.get("name")
        entry: dict[str, Any] = {
            "name": name,
            "description": descriptor.get("description"),
            "exporterIDs": descriptor.get("exporterIDs"),
        }
        counts: dict[str, Any] = {}
        for status in TASK_STATUSES:
            try:
                count = await client.count_queue_tasks(name, status=status)
            except PacsError as exc:
                counts[status] = f"error: {exc}"
                continue
            if count:
                counts[status] = count
        entry["taskCounts"] = counts
        result.append(entry)
    return {"queues": result}


async def list_queue_tasks(
    queue: str, status: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """List tasks in one queue, newest updates first. Use after list_queues or
    get_server_status showed activity/failures in a queue, or to inspect a
    specific export/retrieve job.

    queue: queue name, e.g. Export, Retrieve, HL7Send, StgVerTasks.
    status: optional filter, one of SCHEDULED, IN PROCESS, COMPLETED, WARNING,
    FAILED, CANCELED. Tasks are trimmed to the informative fields (taskID,
    status, failures, timestamps, errorMessage, outcomeMessage, batchID plus
    type-specific fields like RemoteAET/ExporterID/StudyInstanceUID).
    """
    client = _get_client()
    try:
        tasks = await client.list_queue_tasks(queue, status=status, limit=limit)
    except PacsError as exc:
        return _error_dict(exc)
    return {
        "queue": queue,
        "status": status,
        "tasks": [_trim_task(t) for t in tasks],
        "count": len(tasks),
    }


async def get_failed_tasks(limit_per_source: int = 10) -> dict[str, Any]:
    """Collect all FAILED tasks across every default queue and every monitor
    task family (export/retrieve/stgver/diff), grouped by source with
    errorMessage included. This is the primary "what went wrong?" tool on the
    REST side — use it whenever get_server_status reports FAILED tasks, or when
    asked why exports/retrieves/HL7 messages are failing.

    Failed tasks can be retried with retry_task(queue, task_id).
    """
    client = _get_client()
    queues: dict[str, list[dict[str, Any]]] = {}
    monitor: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    total = 0

    for queue in DEFAULT_QUEUES:
        try:
            tasks = await client.list_queue_tasks(
                queue, status="FAILED", limit=limit_per_source
            )
        except PacsError as exc:
            if exc.status_code == 404:
                continue
            errors.append(f"queue {queue}: {exc}")
            continue
        if tasks:
            queues[queue] = [_trim_task(t) for t in tasks]
            total += len(tasks)

    for family in MONITOR_FAMILIES:
        try:
            tasks = await client.list_monitor_tasks(
                family, status="FAILED", limit=limit_per_source
            )
        except PacsError as exc:
            if exc.status_code == 404:
                continue
            errors.append(f"monitor {family}: {exc}")
            continue
        if tasks:
            monitor[family] = [_trim_task(t) for t in tasks]
            total += len(tasks)

    result: dict[str, Any] = {
        "totalFailedFound": total,
        "queues": queues,
        "monitorTasks": monitor,
    }
    if errors:
        result["errors"] = errors
    if total == 0 and not errors:
        result["note"] = "No FAILED tasks found in any queue or monitor task family."
    return result


async def get_storage_status(usable_space_below_gib: float | None = None) -> dict[str, Any]:
    """Show all storage systems of the archive with human-readable free/total
    space and flagged problems (low space, MISSING_OBJECT, FAILED_TO_DELETE,
    ORPHANED, checksum mismatches, ...). Use this when asked about disk space,
    when studies fail to store, or when get_server_status flags a storage.

    usable_space_below_gib: optionally only return storages with less usable
    space than this many GiB.
    """
    client = _get_client()
    usable_space_below = (
        int(usable_space_below_gib * GIB) if usable_space_below_gib is not None else None
    )
    try:
        storages = await client.list_storage(usable_space_below=usable_space_below)
    except PacsError as exc:
        return _error_dict(exc)
    result = []
    for storage in storages:
        result.append(
            {
                "storageID": storage.get("dcmStorageID"),
                "uri": storage.get("dcmURI"),
                "readOnly": storage.get("dcmReadOnly", False),
                "storageDuration": storage.get("dcmStorageDuration"),
                "usage": storage.get("usage"),
                "usableSpace": _human_size(storage.get("usableSpace")),
                "totalSpace": _human_size(storage.get("totalSpace")),
                "usedSpace": _human_size(storage.get("usedSpace")),
                "statusCounts": storage.get("status") or {},
                "problems": _storage_problems(storage),
            }
        )
    return {"storageSystems": result}


async def retry_task(queue: str, task_id: str) -> dict[str, Any]:
    """Reschedule (retry) a task, typically one in FAILED or CANCELED state.
    Use after diagnosing and fixing the cause of a failure found via
    get_failed_tasks / list_queue_tasks. Requires the queue name and taskID
    from those tools.
    """
    client = _get_client()
    try:
        await client.reschedule_task(queue, task_id)
    except PacsError as exc:
        return {"success": False, **_error_dict(exc)}
    return {
        "success": True,
        "action": "reschedule",
        "queue": queue,
        "taskID": task_id,
        "message": f"Task {task_id} in queue {queue} was rescheduled.",
    }


async def cancel_task(queue: str, task_id: str) -> dict[str, Any]:
    """Cancel a pending or running task. Only valid for tasks in SCHEDULED or
    IN PROCESS state — the archive rejects cancelling completed/failed tasks.
    Use when a task is stuck, wrong, or no longer wanted.
    """
    client = _get_client()
    try:
        await client.cancel_task(queue, task_id)
    except PacsError as exc:
        result = {"success": False, **_error_dict(exc)}
        result["hint"] = (
            "Cancel is only valid for tasks in SCHEDULED or IN PROCESS state. "
            + result["hint"]
        )
        return result
    return {
        "success": True,
        "action": "cancel",
        "queue": queue,
        "taskID": task_id,
        "message": f"Task {task_id} in queue {queue} was canceled.",
    }


async def list_open_associations() -> dict[str, Any]:
    """List currently open DICOM associations (live connections from/to
    modalities and other PACS): serial number, connect time, direction, local
    and remote AE titles, performed/invoked operation counts. Use to see live
    DICOM traffic, e.g. an ongoing C-STORE from a modality or a stuck
    association.
    """
    client = _get_client()
    try:
        associations = await client.list_associations()
    except PacsError as exc:
        return _error_dict(exc)
    return {"openAssociations": associations, "count": len(associations)}


def register_tools(mcp: Any) -> None:
    """Register all PACS REST tools on a FastMCP instance."""
    for fn in (
        get_server_status,
        list_aets,
        echo_aet,
        list_queues,
        list_queue_tasks,
        get_failed_tasks,
        get_storage_status,
        retry_task,
        cancel_task,
        list_open_associations,
    ):
        mcp.tool()(fn)
