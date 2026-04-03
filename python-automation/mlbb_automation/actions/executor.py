"""
Appium-based action executor for Android device interaction.

Provides a high-level, resilient API on top of Appium's WebDriver:
  - Coordinate-based actions (tap, swipe, long_press)
  - Text-based element finders (find_element_by_text, find_element_by_id)
  - App management (install_app, launch_app, stop_app, reset_app)
  - Screenshot capture (returns PIL.Image)
  - Context switching for WebView/native (Google Pay flow)
  - Automatic retry on transient Appium exceptions

Usage:
    reserved = farm_client.acquire_device()
    with AppiumExecutor(reserved, settings) as exe:
        exe.launch_app("com.mobile.legends")
        exe.tap(540, 960)
        img = exe.screenshot()
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator, Optional

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from PIL import Image
import io

from ..device_farm.base import ReservedDevice
from ..device_farm.adb_connector import AdbConnector, AdbError
from ..logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

# Exceptions that are safe to retry
_RETRYABLE_EXCEPTIONS = (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)


@dataclass
class MatchResult:
    """Result of finding an element on screen."""

    element: Optional[WebElement]
    x: int
    y: int
    found: bool


class AppiumExecutor:
    """
    Wraps an Appium WebDriver session with retry logic and helper methods.

    Designed to be used as a context manager:
        with AppiumExecutor(reserved, settings) as exe:
            exe.tap(100, 200)
    """

    def __init__(
        self,
        reserved: ReservedDevice,
        retry_count: int = 3,
        retry_delay: float = 2.0,
        action_timeout: int = 30,
        device_id: Optional[str] = None,
        run_logger: Optional[RunLogger] = None,
        adb_key_path: Optional[str] = None,
    ) -> None:
        self._reserved = reserved
        self._retry_count = retry_count
        self._retry_delay = retry_delay
        self._action_timeout = action_timeout
        self._device_id = device_id or reserved.device_info.id
        self._run_logger = run_logger
        self._adb_key_path = adb_key_path
        self._driver: Optional[webdriver.Remote] = None
        self._adb_connector: Optional[AdbConnector] = None
        self._adb_serial: Optional[str] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "AppiumExecutor":
        self.start_session()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.end_session()
        return False  # don't suppress exceptions

    def start_session(self) -> None:
        """
        Connect to the device via ADB (if configured) and start the Appium session.

        When ``ReservedDevice.adb_host`` and ``adb_port`` are set, an ADB TCP
        connection is established first.  The resulting device serial is injected
        into the Appium capabilities as ``udid`` so the Appium server targets the
        correct device.
        """
        # ── 1. ADB connect (Selectel Farm requires this before Appium) ─────────
        if self._reserved.adb_host and self._reserved.adb_port:
            from pathlib import Path
            key_path = Path(self._adb_key_path).expanduser() if self._adb_key_path else None
            self._adb_connector = AdbConnector(key_path=key_path)
            try:
                serial = self._adb_connector.connect(
                    self._reserved.adb_host,
                    self._reserved.adb_port,
                )
                self._adb_serial = serial
                logger.info("ADB connected: %s", serial)
                # Inject UDID so Appium targets the ADB-connected device
                self._reserved.capabilities["udid"] = serial
            except AdbError as exc:
                raise RuntimeError(
                    f"ADB connect to {self._reserved.adb_host}:{self._reserved.adb_port} failed: {exc}. "
                    "Make sure your ADB public key is registered in the Selectel control panel: "
                    "Account → Access → ADB Keys.  "
                    "Run: python -m mlbb_automation setup-adb"
                ) from exc
        else:
            logger.debug(
                "No ADB host/port in ReservedDevice — skipping adb connect. "
                "Appium will connect to udid from capabilities (if any)."
            )

        # ── 2. Appium session ──────────────────────────────────────────────────
        options = UiAutomator2Options()
        for key, value in self._reserved.capabilities.items():
            options.set_capability(key, value)

        logger.info(
            "Starting Appium session",
            appium_url=self._reserved.appium_url,
            device_id=self._device_id,
            adb_serial=self._adb_serial,
        )
        self._driver = webdriver.Remote(
            command_executor=self._reserved.appium_url,
            options=options,
        )
        self._driver.implicitly_wait(5)
        logger.info("Appium session started", session_id=self._driver.session_id)

    def end_session(self) -> None:
        """Quit the Appium session and disconnect from ADB (if connected)."""
        if self._driver:
            try:
                self._driver.quit()
                logger.info("Appium session ended", device_id=self._device_id)
            except Exception as exc:
                logger.warning("Error ending Appium session", error=str(exc))
            finally:
                self._driver = None

        # ── ADB disconnect ─────────────────────────────────────────────────────
        if self._adb_connector and self._adb_serial:
            self._adb_connector.disconnect(self._adb_serial)
            self._adb_connector = None
            self._adb_serial = None

    @property
    def driver(self) -> webdriver.Remote:
        if self._driver is None:
            raise RuntimeError("Appium session not started. Use as context manager or call start_session().")
        return self._driver

    # ------------------------------------------------------------------
    # Structured action logging
    # ------------------------------------------------------------------

    def _record_action(self, action: str, result: str = "ok", **params) -> None:
        """
        Emit a structured action event to both:
          - the module-level structlog logger (debug)
          - the RunLogger JSONL action log (if one was provided)

        Args:
            action: Short action name (e.g. "tap", "swipe", "type_text").
            result: Outcome string — "ok" or a short error description.
            **params: Action parameters (coordinates, text, etc.).
        """
        logger.debug(action, device_id=self._device_id, result=result, **params)
        if self._run_logger is not None:
            self._run_logger.log_action(
                action,
                device_id=self._device_id,
                result=result,
                **params,
            )

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    def screenshot(self) -> Image.Image:
        """Capture a screenshot and return it as a PIL Image."""
        png_bytes = self._retry(lambda: self.driver.get_screenshot_as_png())
        return Image.open(io.BytesIO(png_bytes))

    # ------------------------------------------------------------------
    # Coordinate-based actions
    # ------------------------------------------------------------------

    def tap(self, x: int, y: int, duration_ms: int = 100) -> None:
        """
        Tap at absolute screen coordinates using the W3C Actions API.

        Uses a touch pointer (pointer-down + pause + pointer-up) which is the
        correct approach for Appium 2.x / UiAutomator2. No driver-specific
        ``mobile:`` scripts are used, so this works across server versions.

        Args:
            x:           X coordinate in pixels.
            y:           Y coordinate in pixels.
            duration_ms: Hold duration before releasing (default 100 ms).
        """
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        from selenium.webdriver.common.actions import interaction

        def _do_tap():
            finger = PointerInput(interaction.POINTER_TOUCH, "finger")
            builder = ActionBuilder(self.driver, mouse=finger)
            (
                builder.pointer_action
                .move_to_location(x, y)
                .pointer_down()
                .pause(duration_ms / 1000.0)
                .pointer_up()
            )
            builder.perform()

        self._retry(_do_tap)
        self._record_action("tap", x=x, y=y, duration_ms=duration_ms)

    def long_press(self, x: int, y: int, duration_ms: int = 1500) -> None:
        """
        Long-press at absolute screen coordinates using the W3C Actions API.

        Args:
            x:           X coordinate in pixels.
            y:           Y coordinate in pixels.
            duration_ms: Hold duration in milliseconds (default 1500 ms).
        """
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        from selenium.webdriver.common.actions import interaction

        def _do_long_press():
            finger = PointerInput(interaction.POINTER_TOUCH, "finger")
            builder = ActionBuilder(self.driver, mouse=finger)
            (
                builder.pointer_action
                .move_to_location(x, y)
                .pointer_down()
                .pause(duration_ms / 1000.0)
                .pointer_up()
            )
            builder.perform()

        self._retry(_do_long_press)
        self._record_action("long_press", x=x, y=y, duration_ms=duration_ms)

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> None:
        """
        Swipe from (start_x, start_y) to (end_x, end_y) using the W3C Actions API.

        Uses a pointer-down → pause → move → pointer-up sequence, which is the
        standard drag/swipe gesture for Appium 2.x / UiAutomator2. This avoids
        driver-version-specific ``mobile:`` scripts.

        Args:
            start_x, start_y: Starting pixel coordinates.
            end_x, end_y:     Ending pixel coordinates.
            duration_ms:      Total gesture duration in milliseconds.
        """
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        from selenium.webdriver.common.actions import interaction

        def _do_swipe():
            finger = PointerInput(interaction.POINTER_TOUCH, "finger")
            builder = ActionBuilder(self.driver, mouse=finger)
            (
                builder.pointer_action
                .move_to_location(start_x, start_y)
                .pointer_down()
                .pause(duration_ms / 1000.0)
                .move_to_location(end_x, end_y)
                .pointer_up()
            )
            builder.perform()

        self._retry(_do_swipe)
        self._record_action(
            "swipe",
            start_x=start_x, start_y=start_y,
            end_x=end_x, end_y=end_y,
            duration_ms=duration_ms,
        )

    def swipe_up(self, distance: int = 600) -> None:
        """Convenience: swipe upward from center of screen."""
        size = self.driver.get_window_size()
        cx, cy = size["width"] // 2, size["height"] // 2
        self.swipe(cx, cy, cx, cy - distance)

    def swipe_down(self, distance: int = 600) -> None:
        """Convenience: swipe downward from center of screen."""
        size = self.driver.get_window_size()
        cx, cy = size["width"] // 2, size["height"] // 2
        self.swipe(cx, cy, cx, cy + distance)

    # ------------------------------------------------------------------
    # Key events
    # ------------------------------------------------------------------

    def press_back(self) -> None:
        """Press the Android Back button."""
        self._retry(lambda: self.driver.press_keycode(4))
        self._record_action("press_back")

    def press_home(self) -> None:
        """Press the Android Home button."""
        self._retry(lambda: self.driver.press_keycode(3))
        self._record_action("press_home")

    def press_key(self, keycode: int) -> None:
        """Press an arbitrary Android keycode."""
        self._retry(lambda: self.driver.press_keycode(keycode))
        self._record_action("press_key", keycode=keycode)

    # ------------------------------------------------------------------
    # Text input
    # ------------------------------------------------------------------

    def type_text(self, text: str, clear_first: bool = False) -> None:
        """
        Type text into the currently focused input field.

        Args:
            text:        Text to type.
            clear_first: If True, clear the field before typing.
        """
        if clear_first:
            active = self._retry(lambda: self.driver.switch_to.active_element)
            self._retry(lambda: active.clear())

        def _do_type() -> None:
            try:
                self.driver.execute_script("mobile: type", {"text": text})
            except Exception:
                # Fallback: send_keys on the active element — works across all
                # Appium server versions when mobile: type is unavailable.
                active = self.driver.switch_to.active_element
                active.send_keys(text)

        self._retry(_do_type)
        self._record_action("type_text", text_length=len(text), clear_first=clear_first)

    def type_into_element(self, element: WebElement, text: str, clear_first: bool = True) -> None:
        """Type text into a specific WebElement."""
        if clear_first:
            self._retry(lambda: element.clear())
        self._retry(lambda: element.send_keys(text))
        self._record_action("type_into_element", text_length=len(text), clear_first=clear_first)

    # ------------------------------------------------------------------
    # Element finders
    # ------------------------------------------------------------------

    def find_element_by_text(
        self,
        text: str,
        exact: bool = False,
        timeout: Optional[int] = None,
    ) -> Optional[WebElement]:
        """
        Find an element by its visible text.

        Args:
            text:    Text to search for.
            exact:   If True, match full text; otherwise use 'contains'.
            timeout: Override default action_timeout_seconds.

        Returns:
            The WebElement if found, or None.
        """
        wait = WebDriverWait(self.driver, timeout or self._action_timeout)
        if exact:
            locator = (AppiumBy.XPATH, f'//*[@text="{text}"]')
        else:
            locator = (AppiumBy.XPATH, f'//*[contains(@text, "{text}")]')
        try:
            return wait.until(EC.presence_of_element_located(locator))
        except TimeoutException:
            logger.debug("find_element_by_text: not found", text=text)
            return None

    def find_element_by_id(
        self,
        resource_id: str,
        timeout: Optional[int] = None,
    ) -> Optional[WebElement]:
        """
        Find an element by its Android resource-id.

        Args:
            resource_id: Full resource ID, e.g. 'com.mobile.legends:id/btn_login'.
        """
        wait = WebDriverWait(self.driver, timeout or self._action_timeout)
        try:
            return wait.until(EC.presence_of_element_located((AppiumBy.ID, resource_id)))
        except TimeoutException:
            logger.debug("find_element_by_id: not found", resource_id=resource_id)
            return None

    def find_element_by_content_desc(
        self,
        description: str,
        timeout: Optional[int] = None,
    ) -> Optional[WebElement]:
        """Find an element by its accessibility content description."""
        wait = WebDriverWait(self.driver, timeout or self._action_timeout)
        try:
            return wait.until(
                EC.presence_of_element_located(
                    (AppiumBy.ACCESSIBILITY_ID, description)
                )
            )
        except TimeoutException:
            logger.debug("find_element_by_content_desc: not found", desc=description)
            return None

    def tap_element(self, element: WebElement) -> None:
        """Tap the center of a WebElement."""
        loc = element.location
        size = element.size
        x = loc["x"] + size["width"] // 2
        y = loc["y"] + size["height"] // 2
        self.tap(x, y)

    def tap_by_text(self, text: str, exact: bool = False, timeout: Optional[int] = None) -> bool:
        """
        Find an element by text and tap it.

        Returns:
            True if element was found and tapped, False otherwise.
        """
        el = self.find_element_by_text(text, exact=exact, timeout=timeout)
        if el:
            self.tap_element(el)
            return True
        return False

    def find_element(
        self,
        text: str,
        template_name: Optional[str] = None,
        retries: int = 2,
        retry_delay: float = 1.5,
    ) -> tuple[int, int]:
        """
        Locate a UI element using a 3-stage miss strategy:

        Stage 1 — Template match:
            If ``template_name`` is provided, search the current screenshot
            for the template image.  Returns the match centre if found.

        Stage 2 — OCR text search:
            Run OCR on the current screenshot and look for ``text``.
            Returns the text bounding-box centre if found.

        Stage 3 — Appium UI hierarchy search:
            Query the Appium element tree for a node whose ``@text`` or
            ``@content-desc`` contains ``text``.  Returns the element
            centre if found.

        Each failed stage triggers a 1.5 s pause before the next stage.
        If all three stages miss on the final retry, a diagnostic screenshot
        is saved (when a RunLogger is attached) and a ``RuntimeError`` is
        raised with details from all three stages.

        Args:
            text:          Human-readable label to find via OCR and Appium.
            template_name: Optional template file stem (without extension).
            retries:       Number of full 3-stage attempts before giving up.
            retry_delay:   Seconds between retries.

        Returns:
            (x, y) pixel coordinates of the found element centre.

        Raises:
            RuntimeError: If all stages fail on all retries.
        """
        from ..cv.ocr import OcrEngine
        from ..cv.template_matcher import TemplateMatcher

        ocr_engine = OcrEngine()
        matcher = TemplateMatcher()

        last_errors: list[str] = []

        for attempt in range(1, retries + 1):
            img = self.screenshot()
            stage_errors: list[str] = []

            # Stage 1: template match
            if template_name is not None:
                try:
                    result = matcher.find(img, template_name)
                    if result is not None:
                        self._record_action(
                            "find_element",
                            text=text,
                            stage="template",
                            template=template_name,
                            confidence=round(result.confidence, 3),
                            cx=result.cx,
                            cy=result.cy,
                            attempt=attempt,
                        )
                        return result.cx, result.cy
                    stage_errors.append(
                        f"stage1/template '{template_name}': no match"
                    )
                except Exception as exc:
                    stage_errors.append(f"stage1/template error: {exc}")

            # Stage 2: OCR text search
            try:
                ocr_result = ocr_engine.find_text(img, text, min_confidence=0.5)
                if ocr_result is not None:
                    self._record_action(
                        "find_element",
                        text=text,
                        stage="ocr",
                        matched_text=ocr_result.text,
                        confidence=round(ocr_result.confidence, 3),
                        cx=ocr_result.cx,
                        cy=ocr_result.cy,
                        attempt=attempt,
                    )
                    return ocr_result.cx, ocr_result.cy
                stage_errors.append(f"stage2/ocr '{text}': no match")
            except Exception as exc:
                stage_errors.append(f"stage2/ocr error: {exc}")

            # Stage 3: Appium UI hierarchy — @text attribute
            try:
                el = self.find_element_by_text(text, timeout=5)
                if el is not None:
                    loc = el.location
                    sz = el.size
                    cx = loc["x"] + sz["width"] // 2
                    cy = loc["y"] + sz["height"] // 2
                    self._record_action(
                        "find_element",
                        text=text,
                        stage="appium_text",
                        cx=cx,
                        cy=cy,
                        attempt=attempt,
                    )
                    return cx, cy
                stage_errors.append(f"stage3/appium_text '{text}': not found")
            except Exception as exc:
                stage_errors.append(f"stage3/appium_text error: {exc}")

            # Stage 3b: Appium UI hierarchy — content-desc attribute
            try:
                el = self.find_element_by_content_desc(text, timeout=5)
                if el is not None:
                    loc = el.location
                    sz = el.size
                    cx = loc["x"] + sz["width"] // 2
                    cy = loc["y"] + sz["height"] // 2
                    self._record_action(
                        "find_element",
                        text=text,
                        stage="appium_content_desc",
                        cx=cx,
                        cy=cy,
                        attempt=attempt,
                    )
                    return cx, cy
                stage_errors.append(f"stage3b/appium_content_desc '{text}': not found")
            except Exception as exc:
                stage_errors.append(f"stage3b/appium_content_desc error: {exc}")

            last_errors = stage_errors
            if attempt < retries:
                logger.warning(
                    "find_element retry %d/%d for '%s': %s",
                    attempt,
                    retries,
                    text,
                    "; ".join(stage_errors),
                )
                time.sleep(retry_delay)

        # All retries exhausted — save diagnostic screenshot
        try:
            diag_img = self.screenshot()
            if self._run_logger is not None:
                self._run_logger.save_screenshot(
                    diag_img, label=f"find_element_failed_{text[:30]}"
                )
        except Exception as cap_exc:
            logger.warning("find_element: diagnostic screenshot failed: %s", cap_exc)

        raise RuntimeError(
            f"find_element failed after {retries} attempts for text='{text}' "
            f"template='{template_name}': {'; '.join(last_errors)}"
        )

    # ------------------------------------------------------------------
    # App management
    # ------------------------------------------------------------------

    def install_app(self, apk_path_or_url: str) -> None:
        """
        Install an APK on the device from a local path or a remote URL.

        The Appium UiAutomator2 driver natively supports both local file paths
        and HTTP(S) URLs — pass either and the driver will handle the download
        and installation transparently.

        Args:
            apk_path_or_url: Local path (e.g. '/tmp/app.apk') OR a remote URL
                             (e.g. 'https://example.com/app.apk').
        """
        logger.info("install_app", apk=apk_path_or_url)
        self._retry(lambda: self.driver.install_app(apk_path_or_url))
        self._record_action("install_app", apk=apk_path_or_url)

    def launch_app(self, package: str, activity: Optional[str] = None) -> None:
        """
        Launch an Android app by package name.

        Args:
            package:  Android package name, e.g. 'com.mobile.legends'.
            activity: Optional main activity. If None, system default is used.
        """
        logger.info("launch_app", package=package, activity=activity)
        if activity:
            self.driver.start_activity(package, activity)
        else:
            self._retry(lambda: self.driver.activate_app(package))

    def stop_app(self, package: str) -> None:
        """Force-stop an app."""
        logger.info("stop_app", package=package)
        self._retry(lambda: self.driver.terminate_app(package))

    def reset_app(self, package: str) -> None:
        """Force-stop and clear app data (equivalent to 'Clear Data')."""
        self.stop_app(package)
        self._retry(lambda: self.driver.execute_script(
            "mobile: clearApp", {"appId": package}
        ))

    def is_app_installed(self, package: str) -> bool:
        """Return True if the given package is installed on the device."""
        return self._retry(lambda: self.driver.is_app_installed(package))

    # ------------------------------------------------------------------
    # Context switching (for WebView / Google Pay)
    # ------------------------------------------------------------------

    def get_contexts(self) -> list[str]:
        """Return all available Appium contexts (NATIVE_APP + WEBVIEW_*)."""
        return self._retry(lambda: self.driver.contexts)

    def switch_to_webview(self, timeout: int = 15) -> bool:
        """
        Switch to the first available WEBVIEW context.

        Returns:
            True if successfully switched, False if no WebView found.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            contexts = self.get_contexts()
            webviews = [c for c in contexts if c.startswith("WEBVIEW")]
            if webviews:
                self._retry(lambda: self.driver.switch_to.context(webviews[0]))
                logger.info("Switched to WebView", context=webviews[0])
                return True
            time.sleep(1)
        logger.warning("No WebView context found within %ds", timeout)
        return False

    def switch_to_native(self) -> None:
        """Switch back to the native app context."""
        self._retry(lambda: self.driver.switch_to.context("NATIVE_APP"))
        logger.info("Switched to NATIVE_APP context")

    @contextmanager
    def webview_context(self, timeout: int = 15) -> Generator[bool, None, None]:
        """
        Context manager that switches to WebView and back to native on exit.

        Usage:
            with exe.webview_context() as ok:
                if ok:
                    exe.find_element_by_text("Pay")
        """
        switched = self.switch_to_webview(timeout=timeout)
        try:
            yield switched
        finally:
            self.switch_to_native()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def wait(self, seconds: float) -> None:
        """Explicit sleep — use sparingly; prefer element waits."""
        time.sleep(seconds)

    def get_screen_size(self) -> tuple[int, int]:
        """Return (width, height) of the device screen."""
        size = self.driver.get_window_size()
        return size["width"], size["height"]

    def hide_keyboard(self) -> None:
        """Hide the soft keyboard if visible."""
        try:
            self.driver.hide_keyboard()
        except WebDriverException:
            pass  # keyboard already hidden — safe to ignore

    # ------------------------------------------------------------------
    # Internal retry helper
    # ------------------------------------------------------------------

    def _retry(self, fn, retries: Optional[int] = None, delay: Optional[float] = None):
        """
        Execute fn() with automatic retry on transient Appium exceptions.

        Args:
            fn:      Zero-argument callable to execute.
            retries: Override default retry_count.
            delay:   Override default retry_delay_seconds.

        Returns:
            The return value of fn().

        Raises:
            The last exception if all retries are exhausted.
        """
        max_tries = (retries or self._retry_count) + 1
        wait = delay or self._retry_delay
        last_exc: Optional[Exception] = None

        for attempt in range(1, max_tries + 1):
            try:
                return fn()
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < max_tries:
                    logger.warning(
                        "Retrying Appium action",
                        attempt=attempt,
                        max_tries=max_tries,
                        error=str(exc),
                    )
                    time.sleep(wait * (2 ** (attempt - 1)))  # exponential backoff
                else:
                    # All retries exhausted — capture a screenshot to aid debugging
                    self._capture_failure_screenshot(exc)

        raise last_exc  # type: ignore[misc]

    def _capture_failure_screenshot(self, exc: Exception) -> None:
        """
        Attempt to save a screenshot when all retries are exhausted.

        This is best-effort: screenshot failures are logged as warnings
        and never propagated to the caller.
        """
        if self._run_logger is None:
            return
        try:
            img = self._driver.get_screenshot_as_png() if self._driver else None
            if img:
                from PIL import Image
                import io as _io
                pil_image = Image.open(_io.BytesIO(img))
                label = f"action_failure_{type(exc).__name__}"
                self._run_logger.save_screenshot(pil_image, label=label)
                logger.debug("Saved failure screenshot", label=label)
        except Exception as capture_exc:
            logger.warning(
                "Failed to capture failure screenshot",
                original_error=str(exc),
                capture_error=str(capture_exc),
            )
