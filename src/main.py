"""
Entry point for the DICOM HTJ2K Compression Middleware.

Usage
-----
# Start the DICOMweb STOW-RS proxy (default):
    python -m src.main

# Start the DIMSE C-STORE SCP proxy:
    python -m src.main --mode dimse

# Start both simultaneously:
    python -m src.main --mode both
"""
from __future__ import annotations

import argparse
import sys
import threading

import structlog
import uvicorn

from .middleware.config import settings

log = structlog.get_logger(__name__)


def _start_dicomweb() -> None:
    from .proxies.dicomweb_proxy import app

    uvicorn.run(
        app,
        host=settings.proxy_host,
        port=settings.proxy_port,
        log_level="info",
    )


def _start_dimse() -> None:
    from .proxies.dimse_proxy import DIMSEProxy

    proxy = DIMSEProxy()
    proxy.start(block=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="DICOM HTJ2K compression middleware",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["dicomweb", "dimse", "both"],
        default="dicomweb",
        help=(
            "dicomweb – DICOMweb STOW-RS proxy only; "
            "dimse – C-STORE SCP only; "
            "both – run both concurrently"
        ),
    )
    args = parser.parse_args(argv)

    log.info(
        "Starting DICOM HTJ2K middleware",
        mode=args.mode,
        htj2k_mode=settings.htj2k_mode,
        cloud_pacs=str(settings.cloud_pacs_url),
    )

    if args.mode == "dicomweb":
        _start_dicomweb()

    elif args.mode == "dimse":
        _start_dimse()

    elif args.mode == "both":
        dimse_thread = threading.Thread(target=_start_dimse, daemon=True)
        dimse_thread.start()
        # DICOMweb proxy runs in the main thread (blocks)
        _start_dicomweb()


if __name__ == "__main__":
    main(sys.argv[1:])
