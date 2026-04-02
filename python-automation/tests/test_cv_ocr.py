"""
Unit tests for the OcrEngine (cv/ocr.py).

Tests use synthetic PIL images with text drawn via Pillow so that no
real EasyOCR model download is required — the reader is mocked.
"""

from __future__ import annotations

import io
from typing import List
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw, ImageFont

from mlbb_automation.cv.ocr import OcrEngine, OcrResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_white_image(w: int = 200, h: int = 80) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 255, 255))


def _make_ocr_result(text: str, conf: float, left=10, top=10, right=190, bottom=70) -> OcrResult:
    return OcrResult(
        text=text,
        confidence=conf,
        bbox=(left, top, right, bottom),
        cx=(left + right) // 2,
        cy=(top + bottom) // 2,
    )


def _fake_reader_response(entries):
    """Build a fake EasyOCR readtext return value."""
    results = []
    for text, conf, bbox in entries:
        # bbox in EasyOCR format: list of 4 [x,y] corner points
        left, top, right, bottom = bbox
        points = [[left, top], [right, top], [right, bottom], [left, bottom]]
        results.append((points, text, conf))
    return results


# ---------------------------------------------------------------------------
# OcrResult dataclass
# ---------------------------------------------------------------------------

class TestOcrResult:
    def test_fields_stored_correctly(self):
        r = _make_ocr_result("Hello", 0.9)
        assert r.text == "Hello"
        assert r.confidence == 0.9
        assert r.cx == 100
        assert r.cy == 40

    def test_frozen(self):
        r = _make_ocr_result("test", 0.5)
        with pytest.raises(Exception):
            r.text = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OcrEngine.read_region
# ---------------------------------------------------------------------------

class TestReadRegion:
    def _engine_with_mock_reader(self, fake_results):
        engine = OcrEngine()
        mock_reader = MagicMock()
        mock_reader.readtext.return_value = fake_results
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            return engine, mock_reader

    def test_returns_empty_list_when_no_text(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        mock_reader.readtext.return_value = []
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            results = engine.read_region(_make_white_image())
        assert results == []

    def test_returns_results_sorted_by_confidence(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        fake = _fake_reader_response([
            ("Low", 0.4, (0, 0, 50, 20)),
            ("High", 0.9, (60, 0, 120, 20)),
            ("Mid", 0.7, (130, 0, 190, 20)),
        ])
        mock_reader.readtext.return_value = fake
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            results = engine.read_region(_make_white_image())
        assert [r.text for r in results] == ["High", "Mid", "Low"]

    def test_bbox_offsets_applied_to_cropped_region(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        # OCR returns text at (0,0)→(50,20) within the cropped region
        fake = _fake_reader_response([("Text", 0.8, (0, 0, 50, 20))])
        mock_reader.readtext.return_value = fake
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            # Crop region starts at (100, 200)
            results = engine.read_region(_make_white_image(400, 400), bbox=(100, 200, 300, 350))
        # Coordinates should be offset by (100, 200)
        assert results[0].bbox == (100, 200, 150, 220)
        assert results[0].cx == 125
        assert results[0].cy == 210


# ---------------------------------------------------------------------------
# OcrEngine.find_text
# ---------------------------------------------------------------------------

class TestFindText:
    def test_finds_matching_text(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        fake = _fake_reader_response([("Sign in with Google", 0.85, (10, 10, 200, 50))])
        mock_reader.readtext.return_value = fake
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            result = engine.find_text(_make_white_image(300, 100), "Sign in with Google")
        assert result is not None
        assert "google" in result.text.lower()

    def test_case_insensitive_by_default(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        fake = _fake_reader_response([("SIGN IN WITH GOOGLE", 0.85, (10, 10, 200, 50))])
        mock_reader.readtext.return_value = fake
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            result = engine.find_text(_make_white_image(300, 100), "sign in with google")
        assert result is not None

    def test_returns_none_when_not_found(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        mock_reader.readtext.return_value = _fake_reader_response([
            ("Classic", 0.9, (10, 10, 100, 40)),
        ])
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            result = engine.find_text(_make_white_image(), "Google Pay")
        assert result is None

    def test_below_min_confidence_not_returned(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        fake = _fake_reader_response([("Google Pay", 0.3, (10, 10, 100, 40))])
        mock_reader.readtext.return_value = fake
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            result = engine.find_text(_make_white_image(), "Google Pay", min_confidence=0.7)
        assert result is None

    def test_partial_match_works(self):
        engine = OcrEngine()
        mock_reader = MagicMock()
        fake = _fake_reader_response([("Diamonds Pack 86", 0.9, (10, 10, 200, 50))])
        mock_reader.readtext.return_value = fake
        with patch.object(OcrEngine, "_get_reader", return_value=mock_reader):
            result = engine.find_text(_make_white_image(300, 100), "Diamonds")
        assert result is not None


# ---------------------------------------------------------------------------
# OcrEngine._preprocess
# ---------------------------------------------------------------------------

class TestPreprocess:
    def test_returns_numpy_array(self):
        import numpy as np
        img = _make_white_image()
        arr = OcrEngine._preprocess(img)
        assert isinstance(arr, np.ndarray)
        assert arr.ndim == 2  # Grayscale

    def test_handles_rgba_input(self):
        import numpy as np
        img = Image.new("RGBA", (100, 50), (255, 0, 0, 128))
        arr = OcrEngine._preprocess(img)
        assert arr.ndim == 2
