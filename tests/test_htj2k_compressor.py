"""
Unit tests for the HTJ2K compressor.

These tests use synthetic DICOM datasets so they run without real DICOM files
or a real PACS. imagecodecs must be installed for the encode path to be
exercised; the tests are skipped otherwise.
"""
from __future__ import annotations

import io
import pytest
import numpy as np

pytest.importorskip("pydicom", reason="pydicom required")
imagecodecs = pytest.importorskip("imagecodecs", reason="imagecodecs required for HTJ2K")


def _make_dataset(
    rows: int = 64,
    cols: int = 64,
    bits: int = 16,
    samples: int = 1,
    frames: int = 1,
    signed: bool = False,
) -> "pydicom.Dataset":
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, UID

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT
    ds.file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()

    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = pydicom.uid.generate_uid()
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()

    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 1 if signed else 0
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = "MONOCHROME2" if samples == 1 else "RGB"
    ds.NumberOfFrames = frames

    dtype = np.int16 if signed else (np.uint8 if bits == 8 else np.uint16)
    rng = np.random.default_rng(42)
    pixel_array = rng.integers(0, 2**bits - 1, size=(frames, rows, cols) if frames > 1 else (rows, cols), dtype=dtype)

    if samples > 1:
        pixel_array = np.stack([pixel_array] * samples, axis=-1)

    ds.PixelData = pixel_array.tobytes()
    return ds


class TestHTJ2KCompressor:
    def test_compress_monochrome_ct(self):
        from src.middleware.htj2k_compressor import HTJ2KCompressor
        from src.middleware.config import HTJ2KMode

        ds = _make_dataset(rows=64, cols=64, bits=16)
        compressor = HTJ2KCompressor(mode=HTJ2KMode.LOSSLESS)
        result = compressor.compress_dataset(ds)

        assert result.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.202"
        assert result.PixelData  # non-empty
        # Encapsulated pixel data starts with the Item tag (0xFFFE, 0xE000)
        assert result["PixelData"].is_undefined_length

    def test_compress_multiframe(self):
        from src.middleware.htj2k_compressor import HTJ2KCompressor
        from src.middleware.config import HTJ2KMode

        ds = _make_dataset(rows=32, cols=32, bits=16, frames=4)
        compressor = HTJ2KCompressor(mode=HTJ2KMode.LOSSLESS)
        result = compressor.compress_dataset(ds)

        assert result.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.202"

    def test_transfer_syntax_lossy(self):
        from src.middleware.htj2k_compressor import HTJ2KCompressor
        from src.middleware.config import HTJ2KMode

        ds = _make_dataset(rows=32, cols=32, bits=8)
        compressor = HTJ2KCompressor(mode=HTJ2KMode.LOSSY)
        result = compressor.compress_dataset(ds)

        assert result.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.204"

    def test_no_pixel_data_raises(self):
        from src.middleware.htj2k_compressor import HTJ2KCompressor
        import pydicom

        ds = pydicom.Dataset()
        compressor = HTJ2KCompressor()
        with pytest.raises(ValueError, match="no PixelData"):
            compressor.compress_dataset(ds)
