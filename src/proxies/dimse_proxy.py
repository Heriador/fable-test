"""
DIMSE C-STORE SCP proxy (pynetdicom).

Acts as a C-STORE SCP: receives DICOM instances from any C-STORE SCU,
compresses pixel data to HTJ2K, then:
  - (primary)   forwards via STOW-RS to cloud PACs, OR
  - (secondary) forwards via C-STORE SCU to a cloud PACs DIMSE endpoint.

Run via main.py with --mode dimse.
"""
from __future__ import annotations

import threading
from typing import Optional

import structlog
from pynetdicom import AE, evt, StoragePresentationContexts, ALL_TRANSFER_SYNTAXES
from pynetdicom.sop_class import Verification
from pynetdicom.events import EVT_C_STORE, Event

from ..clients.cloud_pacs_client import CloudPACSClient, CloudPACSError
from ..middleware.config import settings, HTJ2K_TRANSFER_SYNTAXES
from ..middleware.dicom_processor import DicomProcessor

log = structlog.get_logger(__name__)

_DIMSE_STOP_EVENT = threading.Event()


class DIMSEProxy:
    """
    A C-STORE SCP that compresses incoming instances to HTJ2K and forwards
    them to the configured cloud PACs.
    """

    def __init__(self) -> None:
        self._processor = DicomProcessor()
        self._cloud_client = CloudPACSClient()
        self._ae: Optional[AE] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, block: bool = True) -> None:
        """Start the C-STORE SCP. Blocks until stop() is called when block=True."""
        self._ae = self._build_ae()
        log.info(
            "Starting DIMSE C-STORE SCP",
            host=settings.dimse_host,
            port=settings.dimse_port,
            ae_title=settings.dimse_ae_title,
        )
        server = self._ae.start_server(
            (settings.dimse_host, settings.dimse_port),
            block=False,
            ae_title=settings.dimse_ae_title,
        )

        if block:
            try:
                _DIMSE_STOP_EVENT.wait()
            finally:
                server.shutdown()
                log.info("DIMSE SCP stopped")
        return server

    def stop(self) -> None:
        _DIMSE_STOP_EVENT.set()

    # ------------------------------------------------------------------
    # AE construction
    # ------------------------------------------------------------------

    def _build_ae(self) -> AE:
        ae = AE()
        # Accept all standard storage SOP classes with all transfer syntaxes
        for context in StoragePresentationContexts:
            ae.add_supported_context(context.abstract_syntax, ALL_TRANSFER_SYNTAXES)

        # Also support Verification (C-ECHO) for health checks
        ae.add_supported_context(Verification)

        ae.add_requested_presentation_context(Verification)

        handlers = [(EVT_C_STORE, self._handle_c_store)]
        ae.set_network_timeout(60)
        ae.set_acse_timeout(30)
        ae.set_dimse_timeout(120)

        # Bind event handlers by monkey-patching start_server via evt parameter
        self._handlers = handlers
        return ae

    # ------------------------------------------------------------------
    # C-STORE handler
    # ------------------------------------------------------------------

    def _handle_c_store(self, event: Event) -> int:
        """
        Called by pynetdicom for each received C-STORE request.
        Returns a DICOM status code:
          0x0000  Success
          0xA700  Out of Resources
          0xC000  Cannot understand
        """
        from pynetdicom.status import (
            STATUS_SUCCESS,
            STATUS_FAILURE,
        )

        ds = event.dataset
        ds.file_meta = event.file_meta
        sop_uid = str(getattr(ds, "SOPInstanceUID", "unknown"))

        log.info("C-STORE received", sop_uid=sop_uid)

        try:
            import io
            import pydicom
            from pydicom.filebase import DicomBytesIO

            buf = DicomBytesIO()
            pydicom.dcmwrite(buf, ds)
            raw_bytes = buf.getvalue()

            compressed_bytes = self._processor.process_instance(raw_bytes)
            study_uid = str(getattr(ds, "StudyInstanceUID", None))

            self._cloud_client.store_instances(
                [compressed_bytes],
                study_uid=study_uid or None,
            )
            log.info("Instance forwarded to cloud PACs", sop_uid=sop_uid)
            return 0x0000  # Success

        except CloudPACSError as exc:
            log.error("Cloud PACs rejected instance", sop_uid=sop_uid, error=str(exc))
            return 0xA700  # Out of Resources (temporary failure)

        except Exception as exc:
            log.error("Unexpected error in C-STORE handler", sop_uid=sop_uid, error=str(exc))
            return 0xC000  # Cannot understand
