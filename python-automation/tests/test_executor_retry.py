"""
Smoke tests for AppiumExecutor retry logic and failure-screenshot capture.

Uses a mock Appium driver so no real device or Appium server is required.
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from appium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

from mlbb_automation.actions.executor import AppiumExecutor
from mlbb_automation.device_farm.base import DeviceInfo, ReservedDevice
from mlbb_automation.logging.logger import RunLogger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reserved_device() -> ReservedDevice:
    return ReservedDevice(
        device_info=DeviceInfo(
            id="dev-001",
            name="Pixel 7",
            platform="Android",
            platform_version="13",
            model="Pixel 7",
            status="rented",
        ),
        appium_url="https://appium.example.com/wd/hub",
        capabilities={"automationName": "UiAutomator2", "platformName": "Android"},
        session_id="rent-001",
    )


def _make_executor(mock_driver, run_logger=None, retry_count=2):
    """Create an AppiumExecutor with a pre-injected mock driver."""
    reserved = _make_reserved_device()
    exe = AppiumExecutor(
        reserved=reserved,
        retry_count=retry_count,
        retry_delay=0.0,  # no sleep in tests
        run_logger=run_logger,
    )
    exe._driver = mock_driver
    return exe


def _make_run_logger(tmp_path: Path) -> RunLogger:
    return RunLogger(run_id="test-run", log_dir=tmp_path, log_level="DEBUG")


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:
    """
    Test retry behaviour via a helper method that uses execute_script directly
    (avoiding ActionBuilder internals which would require a real WebDriver session).
    """

    def _tap_via_execute_script(self, exe: AppiumExecutor):
        """Use _retry directly to avoid ActionBuilder complexity in unit tests."""
        return exe._retry(lambda: exe.driver.execute_script("mobile: test"))

    def test_succeeds_on_first_try(self, tmp_path):
        driver = MagicMock()
        driver.execute_script.return_value = "ok"
        exe = _make_executor(driver)

        exe._retry(lambda: exe.driver.execute_script("mobile: test"))
        assert driver.execute_script.call_count == 1

    def test_retries_on_stale_element_then_succeeds(self, tmp_path):
        driver = MagicMock()
        call_count = {"n": 0}

        def flaky(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise StaleElementReferenceException("stale")
            return "ok"

        driver.execute_script.side_effect = flaky
        exe = _make_executor(driver, retry_count=3)
        exe._retry(lambda: exe.driver.execute_script("mobile: test"))
        assert driver.execute_script.call_count == 2

    def test_raises_after_all_retries_exhausted(self, tmp_path):
        driver = MagicMock()
        driver.execute_script.side_effect = WebDriverException("connection refused")
        exe = _make_executor(driver, retry_count=2)

        with pytest.raises(WebDriverException):
            exe._retry(lambda: exe.driver.execute_script("mobile: test"))

        # retry_count=2 means 3 total attempts (initial + 2 retries)
        assert driver.execute_script.call_count == 3

    def test_non_retryable_exception_propagates_immediately(self, tmp_path):
        driver = MagicMock()
        driver.execute_script.side_effect = ValueError("not retryable")
        exe = _make_executor(driver, retry_count=3)

        with pytest.raises(ValueError):
            exe._retry(lambda: exe.driver.execute_script("mobile: test"))

        # Should not retry non-retryable exceptions
        assert driver.execute_script.call_count == 1


# ---------------------------------------------------------------------------
# Failure screenshot capture
# ---------------------------------------------------------------------------

class TestFailureScreenshot:
    def _make_1x1_png(self) -> bytes:
        """Return a minimal 1×1 white PNG as bytes."""
        from PIL import Image
        img = Image.new("RGB", (1, 1), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_screenshot_saved_when_retries_exhausted(self, tmp_path):
        driver = MagicMock()
        driver.execute_script.side_effect = WebDriverException("timed out")
        driver.get_screenshot_as_png.return_value = self._make_1x1_png()

        run_logger = _make_run_logger(tmp_path)
        exe = _make_executor(driver, run_logger=run_logger, retry_count=1)

        with pytest.raises(WebDriverException):
            exe._retry(lambda: exe.driver.execute_script("mobile: test"))

        # Screenshot should have been captured
        screenshots = list((tmp_path / "test-run" / "screenshots").glob("*.png"))
        assert screenshots, "Expected at least one failure screenshot"
        assert any("action_failure" in s.name for s in screenshots), \
            f"Screenshot name should contain 'action_failure', got: {[s.name for s in screenshots]}"

    def test_screenshot_not_attempted_without_run_logger(self, tmp_path):
        driver = MagicMock()
        driver.execute_script.side_effect = WebDriverException("timed out")
        driver.get_screenshot_as_png.return_value = self._make_1x1_png()

        exe = _make_executor(driver, run_logger=None, retry_count=1)

        with pytest.raises(WebDriverException):
            exe._retry(lambda: exe.driver.execute_script("mobile: test"))

        # No screenshot call when there's no run_logger
        driver.get_screenshot_as_png.assert_not_called()

    def test_screenshot_failure_does_not_mask_original_exception(self, tmp_path):
        driver = MagicMock()
        driver.execute_script.side_effect = WebDriverException("original error")
        # Make screenshot itself also fail
        driver.get_screenshot_as_png.side_effect = RuntimeError("camera broken")

        run_logger = _make_run_logger(tmp_path)
        exe = _make_executor(driver, run_logger=run_logger, retry_count=1)

        # The original WebDriverException must propagate, not the screenshot error
        with pytest.raises(WebDriverException, match="original error"):
            exe._retry(lambda: exe.driver.execute_script("mobile: test"))


# ---------------------------------------------------------------------------
# find_element — 3-stage miss strategy
# ---------------------------------------------------------------------------

class TestFindElement:
    """
    Tests for AppiumExecutor.find_element() 3-stage fallback.

    Stage 1: TemplateMatcher.find()
    Stage 2: OcrEngine.find_text()
    Stage 3: AppiumExecutor.find_element_by_text() → Appium UI hierarchy
    """

    def _make_1x1_png(self) -> bytes:
        from PIL import Image
        img = Image.new("RGB", (1, 1), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _exe_with_screenshot(self, mock_driver):
        mock_driver.get_screenshot_as_png.return_value = self._make_1x1_png()
        return _make_executor(mock_driver)

    def test_returns_template_coords_when_template_found(self):
        from mlbb_automation.cv.template_matcher import MatchResult
        driver = MagicMock()
        exe = self._exe_with_screenshot(driver)
        tmpl_hit = MatchResult(
            template_name="buy_button",
            cx=300, cy=700,
            confidence=0.92,
            scale=1.0,
            bbox=(280, 685, 320, 715),
        )
        with patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=tmpl_hit), \
             patch("mlbb_automation.cv.ocr.OcrEngine.find_text", return_value=None):
            x, y = exe.find_element("Buy", template_name="buy_button")
        assert (x, y) == (300, 700)

    def test_falls_back_to_ocr_when_template_misses(self):
        from mlbb_automation.cv.ocr import OcrResult
        driver = MagicMock()
        exe = self._exe_with_screenshot(driver)
        ocr_hit = OcrResult(text="Buy", confidence=0.85, cx=200, cy=500, bbox=(180, 490, 220, 510))
        with patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.cv.ocr.OcrEngine.find_text", return_value=ocr_hit):
            x, y = exe.find_element("Buy", template_name="buy_button")
        assert (x, y) == (200, 500)

    def test_falls_back_to_appium_when_ocr_misses(self):
        driver = MagicMock()
        exe = self._exe_with_screenshot(driver)
        mock_el = MagicMock()
        mock_el.location = {"x": 100, "y": 400}
        mock_el.size = {"width": 80, "height": 40}
        with patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.cv.ocr.OcrEngine.find_text", return_value=None), \
             patch.object(exe, "find_element_by_text", return_value=mock_el):
            x, y = exe.find_element("Buy")
        assert (x, y) == (140, 420)

    def test_raises_runtimeerror_when_all_stages_fail(self, tmp_path):
        driver = MagicMock()
        run_logger = _make_run_logger(tmp_path)
        driver.get_screenshot_as_png.return_value = self._make_1x1_png()
        exe = _make_executor(driver, run_logger=run_logger)
        with patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.cv.ocr.OcrEngine.find_text", return_value=None), \
             patch.object(exe, "find_element_by_text", return_value=None):
            with pytest.raises(RuntimeError, match="find_element failed"):
                exe.find_element("NonExistent", template_name="missing_tmpl", retries=1)

    def test_diagnostic_screenshot_saved_on_failure(self, tmp_path):
        driver = MagicMock()
        run_logger = _make_run_logger(tmp_path)
        driver.get_screenshot_as_png.return_value = self._make_1x1_png()
        exe = _make_executor(driver, run_logger=run_logger)
        with patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.cv.ocr.OcrEngine.find_text", return_value=None), \
             patch.object(exe, "find_element_by_text", return_value=None):
            with pytest.raises(RuntimeError):
                exe.find_element("MissingBtn", retries=1)
        screenshots = list((tmp_path / "test-run" / "screenshots").glob("*.png"))
        assert any("find_element_failed" in s.name for s in screenshots)

    def test_skips_template_stage_when_no_template_name(self):
        from mlbb_automation.cv.ocr import OcrResult
        driver = MagicMock()
        exe = self._exe_with_screenshot(driver)
        ocr_hit = OcrResult(text="Shop", confidence=0.9, cx=50, cy=100, bbox=(40, 90, 60, 110))
        with patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find") as mock_tmpl, \
             patch("mlbb_automation.cv.ocr.OcrEngine.find_text", return_value=ocr_hit):
            x, y = exe.find_element("Shop")
        mock_tmpl.assert_not_called()
        assert (x, y) == (50, 100)


class TestActionLogging:
    def test_tap_logged_to_run_logger(self, tmp_path):
        driver = MagicMock()
        driver.execute_script.return_value = None

        run_logger = _make_run_logger(tmp_path)
        exe = _make_executor(driver, run_logger=run_logger)
        exe.tap(42, 84)

        events_path = tmp_path / "test-run" / "events.jsonl"
        assert events_path.exists(), "events.jsonl not created"
        import json
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        action_events = [e for e in events if e.get("action") == "tap"]
        assert action_events, "No tap action event found in events.jsonl"
        ev = action_events[0]
        assert ev["x"] == 42
        assert ev["y"] == 84
        assert ev["result"] == "ok"
