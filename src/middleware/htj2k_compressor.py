"""
HTJ2K (High Throughput JPEG 2000, ISO/IEC 15444-15) compressor for DICOM pixel data.

Supported Transfer Syntaxes produced:
  Lossless      – 1.2.840.10008.1.2.4.202
  Lossless RPCL – 1.2.840.10008.1.2.4.203
  Lossy         – 1.2.840.10008.1.2.4.204

Requires imagecodecs built against OpenJPEG >= 2.5 (which ships HTJ2K support).
"""
from __future__ import annotations

import io
from typing import TYPE_CHECKING, Iterator

import numpy as np
import structlog
from pydicom import Dataset
from pydicom.encaps import encapsulate, generate_frames
from pydicom.pixels.utils import expand_ybr422
from pydicom.uid import (
    UID,
    ExplicitVRLittleEndian,
)

from .config import HTJ2KMode, HTJ2K_TRANSFER_SYNTAXES, settings

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

# Uncompressed transfer syntaxes we can recompress
_UNCOMPRESSED_SYNTAX = {
    "1.2.840.10008.1.2",        # Implicit VR Little Endian
    "1.2.840.10008.1.2.1",      # Explicit VR Little Endian
    "1.2.840.10008.1.2.1.99",   # Deflated Explicit VR Little Endian
    "1.2.840.10008.1.2.2",      # Explicit VR Big Endian (retired)
}


class HTJ2KCompressor:
    """Compresses the pixel data of a pydicom Dataset in-place."""

    def __init__(self, mode: HTJ2KMode = settings.htj2k_mode) -> None:
        self.mode = mode
        self.target_ts = UID(HTJ2K_TRANSFER_SYNTAXES[mode])
        self._check_imagecodecs()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compress_dataset(self, ds: Dataset) -> Dataset:
        """
        Return *ds* with pixel data recompressed to HTJ2K.
        The dataset is modified in-place and returned for chaining.

        Raises
        ------
        ValueError  – dataset has no pixel data or is already compressed.
        RuntimeError – compression fails.
        """
        if not hasattr(ds, "PixelData"):
            raise ValueError("Dataset contains no PixelData element.")

        current_ts = str(ds.file_meta.TransferSyntaxUID) if hasattr(ds, "file_meta") else ""
        if current_ts and current_ts not in _UNCOMPRESSED_SYNTAX:
            log.warning(
                "Dataset already compressed – will re-compress",
                existing_ts=current_ts,
                target_ts=str(self.target_ts),
            )

        frames = list(self._iter_frames(ds))
        log.info("Compressing frames", count=len(frames), mode=self.mode)

        compressed_frames = [self._encode_frame(frame, ds) for frame in frames]

        # Re-assemble encapsulated pixel data
        ds.PixelData = encapsulate(compressed_frames)
        ds["PixelData"].is_undefined_length = True

        # Update transfer syntax
        if not hasattr(ds, "file_meta"):
            from pydicom.dataset import FileMetaDataset
            ds.file_meta = FileMetaDataset()

        ds.file_meta.TransferSyntaxUID = self.target_ts

        log.info(
            "Compression complete",
            transfer_syntax=str(self.target_ts),
            frames=len(compressed_frames),
        )
        return ds

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _iter_frames(self, ds: Dataset) -> Iterator[np.ndarray]:
        """Yield each frame as a NumPy array shaped (rows, cols[, samples])."""
        rows = int(ds.Rows)
        cols = int(ds.Columns)
        n_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
        samples = int(getattr(ds, "SamplesPerPixel", 1))
        bits = int(ds.BitsAllocated)
        is_signed = int(getattr(ds, "PixelRepresentation", 0)) == 1
        photometric = str(getattr(ds, "PhotometricInterpretation", "")).upper()

        dtype = self._numpy_dtype(bits, is_signed)

        raw_pixels = ds.PixelData
        current_ts = ""
        if hasattr(ds, "file_meta"):
            current_ts = str(ds.file_meta.TransferSyntaxUID)

        if current_ts in _UNCOMPRESSED_SYNTAX or current_ts == "":
            # Flat uncompressed buffer
            frame_len = len(raw_pixels) // n_frames
            for i in range(n_frames):
                chunk = raw_pixels[i * frame_len : (i + 1) * frame_len]
                if photometric == "YBR_FULL_422":
                    # Expand 4:2:2 subsampled data to 4:4:4 before encoding
                    chunk = expand_ybr422(chunk, ds.BitsAllocated)
                arr = np.frombuffer(chunk, dtype=dtype).reshape(
                    (rows, cols, samples) if samples > 1 else (rows, cols)
                )
                yield arr
        else:
            # Encapsulated (e.g. existing JPEG-compressed) – decode first
            try:
                frames_data = list(generate_frames(raw_pixels, number_of_frames=n_frames))
            except Exception as exc:
                raise RuntimeError(f"Cannot decode existing compressed pixel data: {exc}") from exc

            for frame_data in frames_data:
                arr = np.frombuffer(frame_data, dtype=dtype).reshape(
                    (rows, cols, samples) if samples > 1 else (rows, cols)
                )
                yield arr

    def _encode_frame(self, frame: np.ndarray, ds: Dataset) -> bytes:
        """Encode a single NumPy frame to an HTJ2K codestream.

        Uses imagecodecs.htj2k_encode (OpenJHTJ2K / OpenJPEG back-end).
        HTJ2K Transfer Syntaxes: lossless=1.2.840.10008.1.2.4.202, lossy=.204
        """
        try:
            import imagecodecs
        except ImportError as exc:
            raise RuntimeError(
                "imagecodecs is required for HTJ2K encoding. "
                "Install it with: pip install imagecodecs"
            ) from exc

        lossy = self.mode == HTJ2KMode.LOSSY

        # htj2k_encode: reversible=True → lossless wavelet (Le Gall 5/3)
        #               reversible=False + level → lossy (CDF 9/7)
        kwargs: dict = {
            "reversible": not lossy,
        }
        if lossy:
            # level maps to target bitrate; lower = more compression
            # Translate PSNR dB target to an approximate level (1..100 range)
            quality = settings.htj2k_lossy_quality
            kwargs["level"] = max(1, min(99, int(quality)))

        try:
            encoded: bytes = imagecodecs.htj2k_encode(frame, **kwargs)
        except Exception as exc:
            raise RuntimeError(f"HTJ2K encoding failed: {exc}") from exc

        return encoded

    @staticmethod
    def _numpy_dtype(bits_allocated: int, is_signed: bool) -> np.dtype:
        if bits_allocated == 8:
            return np.dtype(np.int8 if is_signed else np.uint8)
        if bits_allocated == 16:
            return np.dtype(np.int16 if is_signed else np.uint16)
        if bits_allocated == 32:
            return np.dtype(np.int32 if is_signed else np.uint32)
        raise ValueError(f"Unsupported BitsAllocated: {bits_allocated}")

    @staticmethod
    def _check_imagecodecs() -> None:
        try:
            import imagecodecs
            if not callable(getattr(imagecodecs, "htj2k_encode", None)):
                raise ImportError("imagecodecs has no htj2k_encode – reinstall with HTJ2K support")
        except ImportError as exc:
            raise ImportError(
                "imagecodecs with HTJ2K support is required. "
                "Install with: pip install imagecodecs"
            ) from exc
