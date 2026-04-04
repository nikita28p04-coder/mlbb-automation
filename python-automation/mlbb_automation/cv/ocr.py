"""
OCR module using EasyOCR with lazy initialization.

Features:
  - Lazy reader init (first call only) to avoid 2-3s startup on import
  - Supports Russian + English (covers all MLBB and Google UI text)
  - Preprocessing pipeline: grayscale → CLAHE contrast enhancement → denoising
  - read_region(img, bbox) reads a cropped image region
  - find_text(img, text, min_confidence) scans entire image for a string

Usage:
    engine = OcrEngine()
    results = engine.read_region(pil_image, bbox=(0, 0, 540, 100))
    match = engine.find_text(pil_image, "Sign in with Google", min_confidence=0.6)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from ..logging.logger import get_logger

logger = get_logger(__name__)

BBox = Tuple[int, int, int, int]  # (x, y, w, h) or (left, top, right, bottom)


@dataclass(frozen=True)
class OcrResult:
    """Single OCR detection result."""

    text: str
    confidence: float
    bbox: Tuple[int, int, int, int]  # (left, top, right, bottom) in image coords
    cx: int
    cy: int


class OcrEngine:
    """
    Thread-safe EasyOCR wrapper with lazy reader initialization.

    The reader is only loaded on first use (it pulls ~200MB of weights and
    takes 2-3 seconds). Subsequent calls reuse the cached reader.
    """

    _instance_lock: threading.Lock = threading.Lock()
    _reader = None  # shared across all OcrEngine instances (weights are large)

    def __init__(
        self,
        languages: Optional[List[str]] = None,
        use_gpu: bool = False,
    ) -> None:
        self._languages = languages or ["en", "ru"]
        self._use_gpu = use_gpu

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_region(
        self,
        image: Image.Image,
        bbox: Optional[BBox] = None,
    ) -> List[OcrResult]:
        """
        Run OCR on the full image or a cropped region.

        Args:
            image: PIL Image (any mode; converted internally to RGB).
            bbox:  Optional (left, top, right, bottom) crop rectangle.
                   If None the full image is scanned.

        Returns:
            List of OcrResult sorted by descending confidence.
        """
        if bbox is not None:
            left, top, right, bottom = bbox
            image = image.crop((left, top, right, bottom))
        else:
            left = top = 0

        arr = self._preprocess(image)
        reader = self._get_reader()
        raw = reader.readtext(arr, detail=1, paragraph=False, canvas_size=960)

        results: List[OcrResult] = []
        for points, text, confidence in raw:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            r_left = int(min(xs)) + left
            r_top = int(min(ys)) + top
            r_right = int(max(xs)) + left
            r_bottom = int(max(ys)) + top
            results.append(OcrResult(
                text=text,
                confidence=float(confidence),
                bbox=(r_left, r_top, r_right, r_bottom),
                cx=(r_left + r_right) // 2,
                cy=(r_top + r_bottom) // 2,
            ))
            logger.debug(
                "ocr_result",
                text=text,
                confidence=round(confidence, 3),
                bbox=(r_left, r_top, r_right, r_bottom),
            )

        results.sort(key=lambda r: r.confidence, reverse=True)
        return results

    def find_text(
        self,
        image: Image.Image,
        text: str,
        min_confidence: float = 0.5,
        bbox: Optional[BBox] = None,
        case_sensitive: bool = False,
    ) -> Optional[OcrResult]:
        """
        Find the first occurrence of `text` anywhere in the image (or region).

        Args:
            image:          PIL Image to search.
            text:           Substring to look for.
            min_confidence: Minimum OCR confidence to accept (0.0–1.0).
            bbox:           Optional region to restrict the search.
            case_sensitive: Whether the match is case-sensitive.

        Returns:
            The best-matching OcrResult, or None if not found.
        """
        needle = text if case_sensitive else text.lower()
        results = self.read_region(image, bbox=bbox)

        for r in results:
            if r.confidence < min_confidence:
                continue
            haystack = r.text if case_sensitive else r.text.lower()
            if needle in haystack:
                logger.debug(
                    "ocr_text_found",
                    needle=text,
                    matched=r.text,
                    confidence=round(r.confidence, 3),
                    cx=r.cx,
                    cy=r.cy,
                )
                return r

        logger.debug("ocr_text_not_found", needle=text, min_confidence=min_confidence)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_reader(self):
        """Return (or lazily initialize) the shared EasyOCR reader."""
        if OcrEngine._reader is None:
            with OcrEngine._instance_lock:
                if OcrEngine._reader is None:
                    logger.info("ocr_reader_init", languages=self._languages, gpu=self._use_gpu)
                    import easyocr
                    OcrEngine._reader = easyocr.Reader(
                        self._languages,
                        gpu=self._use_gpu,
                        verbose=False,
                    )
                    logger.info("ocr_reader_ready")
        return OcrEngine._reader

    @staticmethod
    def _preprocess(image: Image.Image) -> np.ndarray:
        """
        Convert PIL image to a preprocessed numpy array for better OCR accuracy.

        Pipeline:
          1. Convert to grayscale
          2. CLAHE contrast enhancement (helps with dark/dim game UI)
          3. Non-local means denoising (reduces compression artefacts)
        """
        import cv2

        arr = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)

        denoised = cv2.fastNlMeansDenoising(enhanced, h=10)

        return denoised
