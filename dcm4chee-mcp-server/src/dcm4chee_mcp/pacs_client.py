"""Async REST client for the dcm4chee-arc 5.33 archive."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

# Default queue names shipped with dcm4chee-arc 5.33.
DEFAULT_QUEUES: tuple[str, ...] = (
    "Export",
    "Retrieve",
    "DiffTasks",
    "StgVerTasks",
    "HL7Send",
    "IANSCU",
    "MPPSSCU",
    "RSClient",
    "Rejection",
    "StgCmtSCP",
    "StgCmtSCU",
)

MONITOR_FAMILIES: tuple[str, ...] = ("export", "retrieve", "stgver", "diff")

TASK_STATUSES: tuple[str, ...] = (
    "SCHEDULED",
    "IN PROCESS",
    "COMPLETED",
    "WARNING",
    "FAILED",
    "CANCELED",
)

# Result codes of POST /aets/{aet}/dimse/{remoteAET} (REST C-ECHO).
ECHO_RESULT_MEANINGS: dict[int, str] = {
    0: "Success",
    1: "IncompatibleConnection",
    2: "FailedToConnect",
    3: "AssociationRejected",
    4: "FailedToSendCEchoRQ",
    5: "FailedToSendCEchoRQ",
    6: "FailedToReceiveCEchoRSP",
    7: "FailedToRelease",
}

# Refresh the cached Keycloak token this many seconds before it expires.
TOKEN_REFRESH_MARGIN_SECONDS = 30.0


class PacsError(Exception):
    """Raised for connection failures and non-2xx responses from the archive."""

    def __init__(self, message: str, *, url: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class Dcm4cheeClient:
    """Thin async wrapper around the dcm4chee-arc 5.33 REST API."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._http = http_client or httpx.AsyncClient(timeout=settings.http_timeout_seconds)
        # Separate client for Keycloak so keycloak_verify_tls only affects token requests.
        self._token_http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._token_http is not None:
            await self._token_http.aclose()

    async def __aenter__(self) -> "Dcm4cheeClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # --- auth ---

    async def _bearer_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        s = self._settings
        if self._token_http is None:
            self._token_http = httpx.AsyncClient(
                timeout=s.http_timeout_seconds, verify=s.keycloak_verify_tls
            )
        assert s.keycloak_token_url is not None
        try:
            resp = await self._token_http.post(
                s.keycloak_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": s.keycloak_client_id or "",
                    "client_secret": s.keycloak_client_secret or "",
                },
            )
        except httpx.HTTPError as exc:
            raise PacsError(
                f"Failed to reach Keycloak token endpoint {s.keycloak_token_url}: {exc!r}",
                url=s.keycloak_token_url,
            ) from exc
        if resp.status_code >= 300:
            raise PacsError(
                f"Keycloak token request to {s.keycloak_token_url} returned "
                f"{resp.status_code}: {resp.text[:500]}",
                url=s.keycloak_token_url,
                status_code=resp.status_code,
            )
        payload = resp.json()
        self._token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 300))
        self._token_expires_at = time.monotonic() + expires_in - TOKEN_REFRESH_MARGIN_SECONDS
        logger.debug("Obtained Keycloak token, expires in %ss", expires_in)
        return self._token

    # --- core request ---

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if self._settings.auth_enabled:
            headers["Authorization"] = f"Bearer {await self._bearer_token()}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.request(method, url, params=query, headers=headers)
        except httpx.HTTPError as exc:
            raise PacsError(f"{method} {url} failed: {exc!r}", url=url) from exc
        if resp.status_code >= 300:
            raise PacsError(
                f"{method} {url} returned {resp.status_code}: {resp.text[:500]}",
                url=str(resp.request.url),
                status_code=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- monitoring ---

    async def get_server_time(self) -> dict[str, Any]:
        return await self._request("GET", "/monitor/serverTime")

    async def list_associations(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/monitor/associations") or []

    # --- queues ---

    async def list_queues(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/queue") or []

    async def list_queue_tasks(
        self,
        queue: str,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        created_time: str | None = None,
        order_by: str = "-updatedTime",
    ) -> list[dict[str, Any]]:
        params = {
            "status": status,
            "limit": limit,
            "offset": offset,
            "createdTime": created_time,
            "orderby": order_by,
        }
        return await self._request("GET", f"/queue/{queue}", params=params) or []

    async def count_queue_tasks(self, queue: str, status: str | None = None) -> int:
        result = await self._request("GET", f"/queue/{queue}/count", params={"status": status})
        return int(result["count"])

    async def reschedule_task(self, queue: str, task_id: str | int) -> None:
        await self._request("POST", f"/queue/{queue}/{task_id}/reschedule")

    async def cancel_task(self, queue: str, task_id: str | int) -> None:
        await self._request("POST", f"/queue/{queue}/{task_id}/cancel")

    # --- monitor task families (export / retrieve / stgver / diff) ---

    async def list_monitor_tasks(
        self,
        family: str,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params = {"status": status, "limit": limit, "offset": offset}
        return await self._request("GET", f"/monitor/{family}", params=params) or []

    async def count_monitor_tasks(self, family: str, status: str | None = None) -> int:
        result = await self._request(
            "GET", f"/monitor/{family}/count", params={"status": status}
        )
        return int(result["count"])

    # --- storage ---

    async def list_storage(self, usable_space_below: int | None = None) -> list[dict[str, Any]]:
        params = {"usableSpaceBelow": usable_space_below}
        return await self._request("GET", "/storage", params=params) or []

    # --- AEs / DIMSE ---

    async def list_aes(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/aes") or []

    async def list_aets(self) -> list[Any]:
        return await self._request("GET", "/aets") or []

    async def echo(
        self,
        remote_aet: str,
        aet: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> dict[str, Any]:
        calling_aet = aet or self._settings.aet
        # 5.33 uses /dimse/, not the legacy /echo/ path.
        return await self._request(
            "POST",
            f"/aets/{calling_aet}/dimse/{remote_aet}",
            params={"host": host, "port": port},
        )
