"""
Unit tests for the Watchdog (scenarios/watchdog.py).

Tests verify:
  - Watchdog starts and stops cleanly
  - OCR-based dismissal fires for known popup text
  - Guard text (buy, pay, etc.) is never tapped
  - Template-based dismissal fires when OCR finds nothing
  - No crash when executor.tap() raises
  - Screen-hash deduplication skips unchanged screens
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest
from PIL import Image

from mlbb_automation.cv.ocr import OcrResult
from mlbb_automation.cv.template_matcher import MatchResult
from mlbb_automation.scenarios.watchdog import Watchdog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _white(w: int = 100, h: int = 100) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 255, 255))


def _make_executor(screenshot: Image.Image = None):
    exe = MagicMock()
    exe.screenshot.return_value = screenshot or _white()
    return exe


def _ocr_hit(text: str, conf: float = 0.85, cx: int = 50, cy: int = 50) -> OcrResult:
    return OcrResult(text=text, confidence=conf, bbox=(10, 10, 90, 90), cx=cx, cy=cy)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestWatchdogLifecycle:
    def test_start_and_stop(self):
        exe = _make_executor()
        # Use a very long poll interval so the thread mostly waits on the stop event
        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None):
            wd = Watchdog(exe, poll_interval=60.0)
            wd.start()
            assert wd._thread is not None
            assert wd._thread.is_alive()
            wd.stop(timeout=3.0)
        assert not wd._thread.is_alive()

    def test_context_manager_starts_and_stops(self):
        exe = _make_executor()
        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None):
            with Watchdog(exe, poll_interval=60.0) as wd:
                assert wd._thread.is_alive()
        assert not wd._thread.is_alive()

    def test_double_start_is_safe(self):
        exe = _make_executor()
        wd = Watchdog(exe, poll_interval=0.05)
        wd.start()
        thread_id = wd._thread.ident
        wd.start()  # should be no-op
        assert wd._thread.ident == thread_id
        wd.stop()


# ---------------------------------------------------------------------------
# OCR-based dismissal
# ---------------------------------------------------------------------------

class TestOcrDismissal:
    def _run_one_check(self, ocr_results, executor=None):
        """Run _check_once with the given OCR results and return the executor."""
        executor = executor or _make_executor()
        wd = Watchdog(executor, poll_interval=999)
        # Patch both OCR and TemplateMatcher so only the OCR path controls behaviour
        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=ocr_results), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None):
            wd._check_once()
        return wd, executor

    def test_dismisses_app_not_responding(self):
        exe = _make_executor()
        hit = _ocr_hit("app not responding", cx=180, cy=1400)
        wd, _ = self._run_one_check([hit], executor=exe)
        exe.tap.assert_called()
        assert wd._dismissed_count == 1

    def test_dismisses_update_later(self):
        exe = _make_executor()
        hit = _ocr_hit("Update Later", cx=540, cy=1300)
        wd, _ = self._run_one_check([hit], executor=exe)
        exe.tap.assert_called_once_with(hit.cx, hit.cy)

    def test_dismisses_cancel(self):
        exe = _make_executor()
        hit = _ocr_hit("cancel", cx=360, cy=1300)
        wd, _ = self._run_one_check([hit], executor=exe)
        exe.tap.assert_called()

    def test_does_not_tap_below_confidence_threshold(self):
        exe = _make_executor()
        low_conf = _ocr_hit("close", conf=0.3)
        wd, _ = self._run_one_check([low_conf], executor=exe)
        exe.tap.assert_not_called()
        assert wd._dismissed_count == 0

    def test_guard_text_never_tapped(self):
        """Payment-related text should never be dismissed."""
        for guard in ("buy", "pay", "purchase", "confirm", "diamonds"):
            exe = _make_executor()
            hit = _ocr_hit(guard)
            wd, _ = self._run_one_check([hit], executor=exe)
            exe.tap.assert_not_called(), f"Should NOT have tapped '{guard}'"

    def test_tap_failure_does_not_raise(self):
        """If executor.tap() raises, watchdog must not crash."""
        exe = _make_executor()
        exe.tap.side_effect = RuntimeError("driver disconnected")
        hit = _ocr_hit("close")
        # Should not raise
        wd, _ = self._run_one_check([hit], executor=exe)


# ---------------------------------------------------------------------------
# Template-based dismissal (OCR finds nothing)
# ---------------------------------------------------------------------------

class TestTemplateDismissal:
    def test_dismisses_via_template_when_ocr_empty(self):
        exe = _make_executor()
        tmpl_hit = MatchResult(
            template_name="close_button",
            cx=1000, cy=200,
            confidence=0.85,
            scale=1.0,
            bbox=(980, 180, 1020, 220),
        )
        wd = Watchdog(exe, poll_interval=999)
        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=tmpl_hit):
            wd._check_once()
        exe.tap.assert_called_once_with(1000, 200)
        assert wd._dismissed_count == 1


# ---------------------------------------------------------------------------
# Screen hash deduplication
# ---------------------------------------------------------------------------

class TestHashDedup:
    def test_same_screen_not_processed_twice(self):
        exe = _make_executor()
        wd = Watchdog(exe, poll_interval=999)

        with patch.object(wd, "_try_dismiss_by_text") as mock_dismiss:
            mock_dismiss.return_value = False
            # First check: new screen → should process
            wd._check_once()
            assert mock_dismiss.call_count == 1

            # Second check: same screen → should skip
            wd._check_once()
            assert mock_dismiss.call_count == 1  # still 1

    def test_different_screens_both_processed(self):
        screen1 = Image.new("RGB", (100, 100), (255, 255, 255))
        screen2 = Image.new("RGB", (100, 100), (0, 0, 0))
        exe = _make_executor()
        exe.screenshot.side_effect = [screen1, screen2]

        wd = Watchdog(exe, poll_interval=999)
        with patch.object(wd, "_try_dismiss_by_text") as mock_dismiss, \
             patch.object(wd, "_try_dismiss_by_template") as mock_tmpl:
            mock_dismiss.return_value = False
            mock_tmpl.return_value = False
            wd._check_once()
            wd._check_once()
            assert mock_dismiss.call_count == 2
