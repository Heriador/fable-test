"""
Cloud PACs client – uploads compressed DICOM instances via DICOMweb STOW-RS
or DIMSE C-STORE (configurable).

STOW-RS spec: https://www.dicomstandard.org/dicomweb/store/
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..middleware.config import CloudPACSAuthScheme, settings

log = structlog.get_logger(__name__)

_MULTIPART_RELATED_CONTENT_TYPE = (
    'multipart/related; type="application/dicom"; boundary="{boundary}"'
)

_STOW_RS_PATH = "/studies"


class CloudPACSError(Exception):
    pass


class CloudPACSClient:
    """
    Sends HTJ2K-compressed DICOM instances to a cloud PACs via DICOMweb STOW-RS.

    The client handles:
      - multipart/related request assembly
      - Basic / Bearer / OAuth2 client-credentials authentication
      - Exponential-backoff retries
    """

    def __init__(self) -> None:
        self._token_cache: Optional[tuple[str, float]] = None  # (token, expiry_epoch)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def store_instances(self, instances: list[bytes], study_uid: Optional[str] = None) -> dict:
        """
        Upload *instances* (compressed DICOM bytes) to the cloud PACs.

        Parameters
        ----------
        instances   List of serialised DICOM SOP instances.
        study_uid   Optional Study UID to scope the STOW-RS URL.

        Returns
        -------
        Parsed JSON response from the PACs (typically a DICOM JSON dataset).
        """
        url = self._build_stow_url(study_uid)
        headers, body = self._build_multipart(instances)
        auth_headers = self._auth_headers()
        headers.update(auth_headers)

        log.info("Sending instances to cloud PACs", url=url, count=len(instances))
        response = self._post_with_retry(url, headers, body)
        log.info("Cloud PACs accepted instances", status=response.status_code)
        return response.json() if response.content else {}

    # ------------------------------------------------------------------
    # Private – request building
    # ------------------------------------------------------------------

    def _build_stow_url(self, study_uid: Optional[str]) -> str:
        base = str(settings.cloud_pacs_url).rstrip("/")
        path = _STOW_RS_PATH
        if study_uid:
            path = f"/studies/{study_uid}"
        return f"{base}{path}"

    @staticmethod
    def _build_multipart(instances: list[bytes]) -> tuple[dict, bytes]:
        """Assemble a multipart/related body and return (headers, body_bytes)."""
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []

        for raw in instances:
            part_header = (
                f"--{boundary}\r\n"
                "Content-Type: application/dicom\r\n"
                "\r\n"
            ).encode()
            parts.append(part_header + raw + b"\r\n")

        closing = f"--{boundary}--\r\n".encode()
        body = b"".join(parts) + closing

        content_type = _MULTIPART_RELATED_CONTENT_TYPE.format(boundary=boundary)
        headers = {
            "Content-Type": content_type,
            "Accept": "application/dicom+json",
        }
        return headers, body

    # ------------------------------------------------------------------
    # Private – authentication
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        scheme = settings.cloud_pacs_auth_scheme
        if scheme == CloudPACSAuthScheme.NONE:
            return {}
        if scheme == CloudPACSAuthScheme.BASIC:
            import base64
            token = base64.b64encode(
                f"{settings.cloud_pacs_username}:{settings.cloud_pacs_password}".encode()
            ).decode()
            return {"Authorization": f"Basic {token}"}
        if scheme == CloudPACSAuthScheme.BEARER:
            return {"Authorization": f"Bearer {settings.cloud_pacs_token}"}
        if scheme == CloudPACSAuthScheme.OAUTH2:
            return {"Authorization": f"Bearer {self._get_oauth2_token()}"}
        return {}

    def _get_oauth2_token(self) -> str:
        """Fetch and cache an OAuth2 client-credentials token."""
        now = time.time()
        if self._token_cache and self._token_cache[1] > now + 30:
            return self._token_cache[0]

        resp = httpx.post(
            str(settings.cloud_pacs_oauth2_token_url),
            data={
                "grant_type": "client_credentials",
                "client_id": settings.cloud_pacs_oauth2_client_id,
                "client_secret": settings.cloud_pacs_oauth2_client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        self._token_cache = (token, now + expires_in)
        return token

    # ------------------------------------------------------------------
    # Private – HTTP with retry
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(settings.http_max_retries),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        reraise=True,
    )
    def _post_with_retry(
        self, url: str, headers: dict, body: bytes
    ) -> httpx.Response:
        with httpx.Client(
            timeout=settings.http_timeout_seconds,
            verify=True,
        ) as client:
            resp = client.post(url, headers=headers, content=body)

        if resp.status_code not in (200, 202):
            raise CloudPACSError(
                f"Cloud PACs returned {resp.status_code}: {resp.text[:400]}"
            )
        return resp
