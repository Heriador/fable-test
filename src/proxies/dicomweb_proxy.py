"""
DICOMweb STOW-RS proxy (FastAPI).

Listens for incoming STOW-RS requests (POST /wado-rs/studies[/{studyUID}]),
compresses pixel data to HTJ2K, then forwards to the cloud PACs.

DICOM PS3.18 §10.5.1 – Store Instances (STOW-RS)
"""
from __future__ import annotations

import re
from typing import Annotated, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from ..clients.cloud_pacs_client import CloudPACSClient, CloudPACSError
from ..middleware.config import settings
from ..middleware.dicom_processor import DicomProcessor

log = structlog.get_logger(__name__)

app = FastAPI(
    title="DICOM HTJ2K Compression Middleware",
    description=(
        "Intercepts DICOM STOW-RS store requests, recompresses pixel data "
        "to HTJ2K, and forwards the study to a cloud PACs."
    ),
    version="1.0.0",
)

_processor = DicomProcessor()
_cloud_client = CloudPACSClient()

# Multipart boundary pattern
_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post(
    f"{settings.proxy_base_path}/studies",
    status_code=status.HTTP_202_ACCEPTED,
    summary="STOW-RS – store instances for any study",
)
async def stow_any_study(request: Request) -> Response:
    return await _handle_stow(request, study_uid=None)


@app.post(
    f"{settings.proxy_base_path}/studies/{{study_uid}}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="STOW-RS – store instances for a specific study",
)
async def stow_study(request: Request, study_uid: str) -> Response:
    return await _handle_stow(request, study_uid=study_uid)


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok", "compression": settings.htj2k_mode}


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------

async def _handle_stow(request: Request, study_uid: Optional[str]) -> Response:
    content_type = request.headers.get("content-type", "")
    if "multipart/related" not in content_type.lower():
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Expected multipart/related content type for STOW-RS.",
        )

    boundary = _extract_boundary(content_type)
    if not boundary:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing multipart boundary in Content-Type header.",
        )

    body = await request.body()
    instances_raw = _split_multipart(body, boundary)

    if not instances_raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No DICOM instances found in multipart body.",
        )

    log.info(
        "Received STOW-RS request",
        study_uid=study_uid or "any",
        instance_count=len(instances_raw),
    )

    # Compress
    try:
        compressed = _processor.process_study(instances_raw)
    except Exception as exc:
        log.error("Compression failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"HTJ2K compression failed: {exc}",
        )

    # Forward to cloud PACs
    try:
        result = _cloud_client.store_instances(compressed, study_uid=study_uid)
    except CloudPACSError as exc:
        log.error("Cloud PACs rejected instances", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cloud PACs error: {exc}",
        )
    except Exception as exc:
        log.error("Unexpected error forwarding to cloud PACs", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    return Response(
        content=str(result).encode(),
        status_code=status.HTTP_202_ACCEPTED,
        media_type="application/dicom+json",
    )


# ---------------------------------------------------------------------------
# Multipart parsing helpers
# ---------------------------------------------------------------------------

def _extract_boundary(content_type: str) -> Optional[str]:
    match = _BOUNDARY_RE.search(content_type)
    return match.group(1).strip() if match else None


def _split_multipart(body: bytes, boundary: str) -> list[bytes]:
    """
    Split a multipart/related body into a list of raw DICOM part bodies.
    Strips MIME headers from each part.
    """
    delimiter = f"--{boundary}".encode()
    closing = f"--{boundary}--".encode()
    parts: list[bytes] = []

    segments = body.split(delimiter)
    for seg in segments:
        seg = seg.strip(b"\r\n")
        if not seg or seg == b"--" or seg.startswith(closing.lstrip(b"--")):
            continue

        # Strip MIME headers (blank line separates headers from body)
        if b"\r\n\r\n" in seg:
            _, dicom_body = seg.split(b"\r\n\r\n", 1)
        elif b"\n\n" in seg:
            _, dicom_body = seg.split(b"\n\n", 1)
        else:
            dicom_body = seg

        dicom_body = dicom_body.rstrip(b"\r\n")
        if dicom_body:
            parts.append(dicom_body)

    return parts
