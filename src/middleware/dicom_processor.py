"""
High-level DICOM study processor: validates, compresses, and serialises datasets.
"""
from __future__ import annotations

import io
from typing import Sequence

import pydicom
import structlog
from pydicom import Dataset, dcmread
from pydicom.filebase import DicomBytesIO

from .htj2k_compressor import HTJ2KCompressor
from .config import settings

log = structlog.get_logger(__name__)


class DicomProcessor:
    """
    Receives raw DICOM bytes (one SOP instance per call), compresses the pixel
    data to HTJ2K and returns the serialised, compressed bytes ready to be
    forwarded to the cloud PACs.
    """

    def __init__(self) -> None:
        self.compressor = HTJ2KCompressor()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process_instance(self, raw_bytes: bytes) -> bytes:
        """
        Parse *raw_bytes* as a DICOM SOP instance, compress its pixel data
        to HTJ2K, and return the serialised result.

        Instances that carry no pixel data (e.g. SR, KO, PR) are returned
        unchanged to preserve round-trip fidelity.
        """
        ds = self._parse(raw_bytes)
        sop_uid = str(getattr(ds, "SOPInstanceUID", "unknown"))
        sop_class = str(getattr(ds, "SOPClassUID", "unknown"))

        log.info("Processing instance", sop_uid=sop_uid, sop_class=sop_class)

        if not hasattr(ds, "PixelData"):
            log.info("Instance has no pixel data – forwarding as-is", sop_uid=sop_uid)
            return raw_bytes

        ds = self.compressor.compress_dataset(ds)
        return self._serialise(ds)

    def process_study(self, instances: Sequence[bytes]) -> list[bytes]:
        """Batch-process an entire study (list of raw DICOM bytes)."""
        results = []
        for raw in instances:
            try:
                results.append(self.process_instance(raw))
            except Exception as exc:
                log.error("Failed to process instance", error=str(exc))
                raise
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(raw_bytes: bytes) -> Dataset:
        try:
            return dcmread(DicomBytesIO(raw_bytes), force=True)
        except Exception as exc:
            raise ValueError(f"Cannot parse DICOM bytes: {exc}") from exc

    @staticmethod
    def _serialise(ds: Dataset) -> bytes:
        buf = DicomBytesIO()
        # enforce_file_format writes a proper DICOM Part 10 preamble + file meta
        pydicom.dcmwrite(buf, ds, enforce_file_format=True)
        return buf.getvalue()
