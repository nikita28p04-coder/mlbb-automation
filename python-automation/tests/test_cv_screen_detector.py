"""
Unit tests for ScreenDetector (cv/screen_detector.py).

Uses mocked OcrEngine and TemplateMatcher so no real screenshots are needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from mlbb_automation.cv.ocr import OcrResult
from mlbb_automation.cv.screen_detector import ScreenDetector, ScreenState
from mlbb_automation.cv.template_matcher import MatchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _white(w: int = 1080, h: int = 1920) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 255, 255))


def _ocr_hit(text: str, conf: float = 0.9) -> OcrResult:
    return OcrResult(text=text, confidence=conf, bbox=(10, 10, 200, 50), cx=105, cy=30)


def _tmpl_hit(name: str, conf: float = 0.85) -> MatchResult:
    return MatchResult(template_name=name, cx=100, cy=100, confidence=conf, scale=1.0, bbox=(50, 80, 150, 120))


def _make_detector(ocr_results=None, tmpl_results=None) -> ScreenDetector:
    """
    Build a ScreenDetector where OCR always returns ocr_results (flat list
    across all read_region calls) and TemplateMatcher.find returns tmpl_results
    dict keyed by template name.
    """
    mock_ocr = MagicMock()
    mock_ocr.read_region.return_value = ocr_results or []

    tmpl_results = tmpl_results or {}
    mock_matcher = MagicMock()
    mock_matcher.find.side_effect = lambda img, name, threshold=0.8: tmpl_results.get(name)

    return ScreenDetector(ocr=mock_ocr, matcher=mock_matcher)


# ---------------------------------------------------------------------------
# ScreenState enum
# ---------------------------------------------------------------------------

class TestScreenStateEnum:
    def test_all_states_defined(self):
        expected = {
            "GOOGLE_LOGIN", "GOOGLE_2FA",
            "MLBB_LOADING", "MLBB_MAIN_MENU",
            "MLBB_SHOP", "MLBB_SHOP_DIAMONDS",
            "MLBB_PAYMENT", "GOOGLE_PAY_SHEET",
            "PAYMENT_SUCCESS", "PAYMENT_FAILED",
            "UNKNOWN",
        }
        actual = {s.name for s in ScreenState}
        assert expected == actual


# ---------------------------------------------------------------------------
# Detection scenarios
# ---------------------------------------------------------------------------

class TestScreenDetector:
    def test_payment_success_detected_by_ocr(self):
        detector = _make_detector(ocr_results=[_ocr_hit("Payment Successful")])
        state = detector.detect(_white())
        assert state == ScreenState.PAYMENT_SUCCESS

    def test_payment_failed_detected_by_ocr(self):
        detector = _make_detector(ocr_results=[_ocr_hit("Payment Failed")])
        state = detector.detect(_white())
        assert state == ScreenState.PAYMENT_FAILED

    def test_google_pay_sheet_detected_by_template(self):
        detector = _make_detector(
            ocr_results=[_ocr_hit("Pay")],
            tmpl_results={"google_pay_logo": _tmpl_hit("google_pay_logo")},
        )
        state = detector.detect(_white())
        assert state == ScreenState.GOOGLE_PAY_SHEET

    def test_google_pay_sheet_detected_by_ocr_only(self):
        detector = _make_detector(
            ocr_results=[_ocr_hit("Google Pay"), _ocr_hit("Buy with")],
        )
        state = detector.detect(_white())
        assert state == ScreenState.GOOGLE_PAY_SHEET

    def test_mlbb_shop_diamonds_detected(self):
        detector = _make_detector(
            ocr_results=[_ocr_hit("Diamonds"), _ocr_hit("Buy")],
        )
        state = detector.detect(_white())
        assert state == ScreenState.MLBB_SHOP_DIAMONDS

    def test_mlbb_shop_detected_by_template(self):
        detector = _make_detector(
            tmpl_results={"shop_icon": _tmpl_hit("shop_icon")},
        )
        state = detector.detect(_white())
        assert state == ScreenState.MLBB_SHOP

    def test_mlbb_main_menu_detected(self):
        detector = _make_detector(
            ocr_results=[_ocr_hit("Classic"), _ocr_hit("Ranked")],
        )
        state = detector.detect(_white())
        assert state == ScreenState.MLBB_MAIN_MENU

    def test_mlbb_loading_detected(self):
        detector = _make_detector(
            ocr_results=[_ocr_hit("Loading"), _ocr_hit("Mobile Legends")],
        )
        state = detector.detect(_white())
        assert state == ScreenState.MLBB_LOADING

    def test_google_2fa_detected(self):
        detector = _make_detector(
            ocr_results=[_ocr_hit("2-Step Verification")],
        )
        state = detector.detect(_white())
        assert state == ScreenState.GOOGLE_2FA

    def test_google_login_detected_by_template(self):
        detector = _make_detector(
            tmpl_results={"google_sign_in_button": _tmpl_hit("google_sign_in_button")},
        )
        state = detector.detect(_white())
        assert state == ScreenState.GOOGLE_LOGIN

    def test_google_login_detected_by_email_prompt(self):
        detector = _make_detector(
            ocr_results=[_ocr_hit("Enter your email")],
        )
        state = detector.detect(_white())
        assert state == ScreenState.GOOGLE_LOGIN

    def test_unknown_when_no_signals_match(self):
        detector = _make_detector(ocr_results=[], tmpl_results={})
        state = detector.detect(_white())
        assert state == ScreenState.UNKNOWN

    def test_low_confidence_ocr_not_counted(self):
        """OCR results below min_confidence should not trigger a state."""
        low_conf_result = OcrResult(
            text="Payment Successful",
            confidence=0.1,  # below 0.5 threshold
            bbox=(0, 0, 100, 30),
            cx=50, cy=15,
        )
        detector = _make_detector(ocr_results=[low_conf_result])
        state = detector.detect(_white())
        assert state == ScreenState.UNKNOWN
