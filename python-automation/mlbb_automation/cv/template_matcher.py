"""
Template matching using OpenCV's matchTemplate with multi-scale support.

Loads PNG reference templates from the templates/ directory.
Searches across multiple scales (0.7x–1.3x) to handle different device resolutions.
Returns a MatchResult with center coordinates and confidence, or None below threshold.

Usage:
    matcher = TemplateMatcher(templates_dir=Path("mlbb_automation/templates"))
    result = matcher.find(screenshot_pil, "shop_icon", threshold=0.8)
    if result:
        exe.tap(result.cx, result.cy)
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from ..logging.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

_SCALES = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
_DEFAULT_THRESHOLD = 0.80


@dataclass(frozen=True)
class MatchResult:
    """Result of a successful template match."""

    template_name: str
    cx: int
    cy: int
    confidence: float
    scale: float
    bbox: tuple  # (left, top, right, bottom)


class TemplateMatcher:
    """
    Multi-scale template matcher backed by OpenCV.

    Templates are loaded lazily from `templates_dir` on first use and cached
    in-process.  Matching is always done in grayscale for speed and robustness.
    """

    def __init__(
        self,
        templates_dir: Optional[Path] = None,
        default_threshold: float = _DEFAULT_THRESHOLD,
    ) -> None:
        self._templates_dir = templates_dir or _DEFAULT_TEMPLATES_DIR
        self._default_threshold = default_threshold
        self._cache: dict = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find(
        self,
        screenshot: Image.Image,
        template_name: str,
        threshold: Optional[float] = None,
    ) -> Optional[MatchResult]:
        """
        Search for a template in the screenshot using multi-scale matching.

        Args:
            screenshot:    Full-screen PIL Image.
            template_name: Filename stem in templates_dir (e.g. "shop_icon"
                           will load "shop_icon.png").
            threshold:     Minimum acceptable confidence (0.0–1.0).
                           Defaults to default_threshold (0.8).

        Returns:
            MatchResult if found above threshold, else None.
        """
        threshold = threshold if threshold is not None else self._default_threshold

        tmpl_gray = self._load_template(template_name)
        if tmpl_gray is None:
            logger.warning(
                "template_not_found",
                template=template_name,
                templates_dir=str(self._templates_dir),
            )
            return None

        screen_gray = self._to_gray(screenshot)
        best = self._match_multiscale(screen_gray, tmpl_gray, template_name, threshold)

        if best is None:
            logger.debug(
                "template_match_miss",
                template=template_name,
                threshold=threshold,
            )
        else:
            logger.debug(
                "template_match_hit",
                template=template_name,
                confidence=round(best.confidence, 4),
                cx=best.cx,
                cy=best.cy,
                scale=best.scale,
            )

        return best

    def find_all(
        self,
        screenshot: Image.Image,
        template_name: str,
        threshold: Optional[float] = None,
    ) -> list:
        """
        Find all non-overlapping occurrences of a template using NMS.

        Returns:
            List of MatchResult, sorted by descending confidence.
        """
        threshold = threshold if threshold is not None else self._default_threshold

        tmpl_gray = self._load_template(template_name)
        if tmpl_gray is None:
            return []

        screen_gray = self._to_gray(screenshot)
        th, tw = tmpl_gray.shape[:2]
        results = []

        for scale in _SCALES:
            scaled_tmpl = self._scale_template(tmpl_gray, scale)
            if scaled_tmpl is None:
                continue
            st, sw = scaled_tmpl.shape[:2]
            if st > screen_gray.shape[0] or sw > screen_gray.shape[1]:
                continue

            res = cv2.matchTemplate(screen_gray, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
            locs = np.where(res >= threshold)
            for pt_y, pt_x in zip(*locs):
                cx = int(pt_x) + sw // 2
                cy = int(pt_y) + st // 2
                conf = float(res[pt_y, pt_x])
                results.append(MatchResult(
                    template_name=template_name,
                    cx=cx, cy=cy,
                    confidence=conf,
                    scale=scale,
                    bbox=(int(pt_x), int(pt_y), int(pt_x) + sw, int(pt_y) + st),
                ))

        # Non-maximum suppression: remove overlapping boxes, keep highest conf
        results = self._nms(results, iou_threshold=0.4)
        results.sort(key=lambda r: r.confidence, reverse=True)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_template(self, name: str) -> Optional[np.ndarray]:
        """Load and cache a grayscale template array."""
        if name in self._cache:
            return self._cache[name]

        path = self._templates_dir / f"{name}.png"
        if not path.exists():
            self._cache[name] = None
            return None

        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            logger.warning("template_load_failed", path=str(path))
            self._cache[name] = None
            return None

        self._cache[name] = img
        logger.debug("template_loaded", name=name, shape=img.shape)
        return img

    @staticmethod
    def _to_gray(image: Image.Image) -> np.ndarray:
        arr = np.array(image.convert("RGB"))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    @staticmethod
    def _scale_template(tmpl: np.ndarray, scale: float) -> Optional[np.ndarray]:
        """Return a resized copy of the template, or None if too small."""
        h, w = tmpl.shape[:2]
        new_h, new_w = max(1, int(h * scale)), max(1, int(w * scale))
        if new_h < 5 or new_w < 5:
            return None
        return cv2.resize(tmpl, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    def _match_multiscale(
        self,
        screen: np.ndarray,
        tmpl: np.ndarray,
        template_name: str,
        threshold: float,
    ) -> Optional[MatchResult]:
        """Return the best match across all scales, or None if below threshold."""
        best_conf = -1.0
        best_result = None

        for scale in _SCALES:
            scaled = self._scale_template(tmpl, scale)
            if scaled is None:
                continue
            st, sw = scaled.shape[:2]
            if st > screen.shape[0] or sw > screen.shape[1]:
                continue

            res = cv2.matchTemplate(screen, scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_conf:
                best_conf = max_val
                if max_val >= threshold:
                    pt_x, pt_y = max_loc
                    cx = pt_x + sw // 2
                    cy = pt_y + st // 2
                    best_result = MatchResult(
                        template_name=template_name,
                        cx=cx, cy=cy,
                        confidence=max_val,
                        scale=scale,
                        bbox=(pt_x, pt_y, pt_x + sw, pt_y + st),
                    )

        return best_result

    @staticmethod
    def _nms(results: list, iou_threshold: float) -> list:
        """Simple greedy non-maximum suppression by IoU."""
        if not results:
            return []
        results = sorted(results, key=lambda r: r.confidence, reverse=True)
        kept = []
        for candidate in results:
            dominated = False
            for keeper in kept:
                if TemplateMatcher._iou(candidate.bbox, keeper.bbox) > iou_threshold:
                    dominated = True
                    break
            if not dominated:
                kept.append(candidate)
        return kept

    @staticmethod
    def _iou(a: tuple, b: tuple) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        inter_x1 = max(ax1, bx1)
        inter_y1 = max(ay1, by1)
        inter_x2 = min(ax2, bx2)
        inter_y2 = min(ay2, by2)
        inter_area = max(0, inter_x2 - inter_x1) * max(0, inter_y2 - inter_y1)
        if inter_area == 0:
            return 0.0
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        return inter_area / (area_a + area_b - inter_area)
