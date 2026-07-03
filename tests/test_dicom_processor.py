"""
Tests for DicomProcessor – focuses on the serialise/parse round-trip
and the no-pixel-data pass-through path.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pydicom")


def _make_raw_ct() -> bytes:
    """Return a minimal CT SOP instance as raw bytes."""
    import pydicom
    import numpy as np
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian
    from pydicom.filebase import DicomBytesIO

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()

    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = pydicom.uid.generate_uid()
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.Rows = 32
    ds.Columns = 32
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"

    rng = np.random.default_rng(0)
    ds.PixelData = rng.integers(0, 65535, size=(32, 32), dtype=np.uint16).tobytes()

    buf = DicomBytesIO()
    pydicom.dcmwrite(buf, ds)
    return buf.getvalue()


def _make_raw_sr() -> bytes:
    """Return a Structured Report (no pixel data) as raw bytes."""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian
    from pydicom.filebase import DicomBytesIO

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.22"
    ds.file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()

    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.22"
    ds.SOPInstanceUID = pydicom.uid.generate_uid()
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()

    buf = DicomBytesIO()
    pydicom.dcmwrite(buf, ds)
    return buf.getvalue()


class TestDicomProcessor:
    def test_sr_pass_through(self):
        """Instances with no pixel data must be returned unchanged."""
        from unittest.mock import patch

        sr_raw = _make_raw_sr()

        # Processor should return original bytes without calling compressor
        from src.middleware.dicom_processor import DicomProcessor

        proc = DicomProcessor()
        result = proc.process_instance(sr_raw)
        assert result == sr_raw

    @pytest.mark.skipif(
        not pytest.importorskip("imagecodecs", reason="skip"),
        reason="imagecodecs required",
    )
    def test_ct_compressed(self):
        """CT instance gets its transfer syntax updated to HTJ2K Lossless."""
        import pydicom
        from pydicom.filebase import DicomBytesIO
        from src.middleware.dicom_processor import DicomProcessor

        raw = _make_raw_ct()
        proc = DicomProcessor()
        result_bytes = proc.process_instance(raw)

        result_ds = pydicom.dcmread(DicomBytesIO(result_bytes))
        assert str(result_ds.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.4.202"

    def test_garbage_bytes_passed_through(self):
        """
        pydicom's force=True parser is lenient; unrecognised bytes with no
        PixelData element should be forwarded unchanged (as a non-image instance).
        """
        from src.middleware.dicom_processor import DicomProcessor

        proc = DicomProcessor()
        raw = b"not dicom data at all !!!"
        # Should not raise – treated as a no-pixel-data instance
        result = proc.process_instance(raw)
        assert result == raw
