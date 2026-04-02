"""
Unit tests for TemplateMatcher (cv/template_matcher.py).

Tests create synthetic grayscale images (solid-colour squares) to verify
matching logic without requiring real game screenshots.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import tempfile

import cv2
import numpy as np
import pytest
from PIL import Image

from mlbb_automation.cv.template_matcher import MatchResult, TemplateMatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pil(w: int, h: int, color=(200, 200, 200)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def _make_gray_array(w: int, h: int, value: int = 128) -> np.ndarray:
    return np.full((h, w), value, dtype=np.uint8)


def _write_template(directory: Path, name: str, w: int = 32, h: int = 16, value: int = 100):
    """Write a solid-colour grayscale PNG template to disk."""
    arr = _make_gray_array(w, h, value)
    path = directory / f"{name}.png"
    cv2.imwrite(str(path), arr)
    return path


# ---------------------------------------------------------------------------
# MatchResult dataclass
# ---------------------------------------------------------------------------

class TestMatchResult:
    def test_fields_accessible(self):
        r = MatchResult(
            template_name="test",
            cx=50, cy=30,
            confidence=0.92,
            scale=1.0,
            bbox=(10, 10, 90, 50),
        )
        assert r.template_name == "test"
        assert r.confidence == 0.92
        assert r.cx == 50


# ---------------------------------------------------------------------------
# TemplateMatcher.find — hit cases
# ---------------------------------------------------------------------------

class TestTemplateMatcherFind:
    def test_finds_exact_match_at_scale_1(self, tmp_path):
        """A template that exactly appears in the screenshot should be found."""
        # Use a gradient template so that there is only one strong match position
        tmpl_w, tmpl_h = 30, 15
        tmpl_arr = np.zeros((tmpl_h, tmpl_w), dtype=np.uint8)
        # Gradient: left=0, right=255
        for x in range(tmpl_w):
            tmpl_arr[:, x] = int(x / (tmpl_w - 1) * 255)

        # Build a screenshot: uniform background except the template pasted at (80, 60)
        screen_arr = np.full((120, 200), 128, dtype=np.uint8)
        screen_arr[60:60+tmpl_h, 80:80+tmpl_w] = tmpl_arr
        screen_pil = Image.fromarray(screen_arr)

        # Write template
        path = tmp_path / "patch.png"
        cv2.imwrite(str(path), tmpl_arr)

        matcher = TemplateMatcher(templates_dir=tmp_path, default_threshold=0.80)
        result = matcher.find(screen_pil, "patch")

        assert result is not None, "Expected a match but got None"
        assert abs(result.cx - (80 + tmpl_w // 2)) <= 5, f"cx={result.cx}, expected ~{80 + tmpl_w//2}"
        assert abs(result.cy - (60 + tmpl_h // 2)) <= 5, f"cy={result.cy}, expected ~{60 + tmpl_h//2}"
        assert result.confidence >= 0.80

    def test_find_returns_none_when_template_missing(self, tmp_path):
        """Non-existent template file → None, no exception."""
        matcher = TemplateMatcher(templates_dir=tmp_path)
        result = matcher.find(_make_pil(200, 100), "nonexistent_template")
        assert result is None

    def test_find_returns_none_below_threshold(self, tmp_path):
        """Low-similarity: gradient template vs uniform screen → near-zero confidence."""
        # A gradient template has zero correlation with a uniform background
        tmpl_arr = np.zeros((20, 40), dtype=np.uint8)
        for x in range(40):
            tmpl_arr[:, x] = int(x / 39 * 255)
        path = tmp_path / "patch.png"
        cv2.imwrite(str(path), tmpl_arr)

        # Uniform screen — no gradient anywhere → confidence ~0
        screen = Image.fromarray(_make_gray_array(200, 100, 128))
        matcher = TemplateMatcher(templates_dir=tmp_path, default_threshold=0.80)
        result = matcher.find(screen, "patch")
        assert result is None

    def test_threshold_override(self, tmp_path):
        """Passing threshold=0.0 should always find something."""
        _write_template(tmp_path, "patch", 20, 10, value=100)
        screen = _make_pil(100, 50)
        matcher = TemplateMatcher(templates_dir=tmp_path)
        result = matcher.find(screen, "patch", threshold=0.0)
        assert result is not None


# ---------------------------------------------------------------------------
# TemplateMatcher.find_all
# ---------------------------------------------------------------------------

class TestFindAll:
    def test_finds_multiple_occurrences(self, tmp_path):
        """Two identical gradient patches → find_all returns 2 results."""
        tmpl_w, tmpl_h = 20, 10
        # Gradient template: distinct pattern to avoid uniform-match explosion
        tmpl_arr = np.zeros((tmpl_h, tmpl_w), dtype=np.uint8)
        for x in range(tmpl_w):
            tmpl_arr[:, x] = int(x / (tmpl_w - 1) * 200)

        screen_arr = np.full((100, 300), 100, dtype=np.uint8)
        # Place two copies far apart (separated by at least tmpl_w so NMS keeps both)
        screen_arr[10:10+tmpl_h, 20:20+tmpl_w] = tmpl_arr
        screen_arr[10:10+tmpl_h, 200:200+tmpl_w] = tmpl_arr
        screen_pil = Image.fromarray(screen_arr)

        path = tmp_path / "patch.png"
        cv2.imwrite(str(path), tmpl_arr)
        matcher = TemplateMatcher(templates_dir=tmp_path, default_threshold=0.80)
        results = matcher.find_all(screen_pil, "patch")

        assert len(results) >= 2, f"Expected >=2 results, got {len(results)}"

    def test_returns_empty_list_when_no_match(self, tmp_path):
        """Gradient template in empty screen → no match above threshold."""
        tmpl_arr = np.zeros((10, 20), dtype=np.uint8)
        for x in range(20):
            tmpl_arr[:, x] = int(x / 19 * 255)
        path = tmp_path / "patch.png"
        cv2.imwrite(str(path), tmpl_arr)
        screen = Image.fromarray(_make_gray_array(100, 50, 128))  # uniform screen
        matcher = TemplateMatcher(templates_dir=tmp_path, default_threshold=0.80)
        results = matcher.find_all(screen, "patch")
        assert results == []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class TestInternals:
    def test_iou_overlapping(self):
        a = (0, 0, 10, 10)
        b = (5, 5, 15, 15)
        iou = TemplateMatcher._iou(a, b)
        assert 0.1 < iou < 0.5

    def test_iou_non_overlapping(self):
        a = (0, 0, 10, 10)
        b = (20, 20, 30, 30)
        assert TemplateMatcher._iou(a, b) == 0.0

    def test_iou_identical(self):
        a = (0, 0, 10, 10)
        assert TemplateMatcher._iou(a, a) == pytest.approx(1.0)

    def test_scale_template_returns_none_for_tiny(self):
        tmpl = _make_gray_array(10, 10)
        result = TemplateMatcher._scale_template(tmpl, 0.1)
        assert result is None

    def test_template_caching(self, tmp_path):
        """Template should only be read from disk once."""
        _write_template(tmp_path, "cached", 20, 10)
        matcher = TemplateMatcher(templates_dir=tmp_path)
        screen = _make_pil(100, 50)
        matcher.find(screen, "cached")
        matcher.find(screen, "cached")  # second call should use cache
        assert "cached" in matcher._cache
