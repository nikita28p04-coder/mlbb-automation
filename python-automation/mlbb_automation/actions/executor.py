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
from ..logging.logger import get_logger

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
    ) -> None:
        self._reserved = reserved
        self._retry_count = retry_count
        self._retry_delay = retry_delay
        self._action_timeout = action_timeout
        self._device_id = device_id or reserved.device_info.id
        self._driver: Optional[webdriver.Remote] = None

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
        """Create the Appium WebDriver session."""
        options = UiAutomator2Options()
        for key, value in self._reserved.capabilities.items():
            options.set_capability(key, value)

        logger.info(
            "Starting Appium session",
            appium_url=self._reserved.appium_url,
            device_id=self._device_id,
        )
        self._driver = webdriver.Remote(
            command_executor=self._reserved.appium_url,
            options=options,
        )
        self._driver.implicitly_wait(5)
        logger.info("Appium session started", session_id=self._driver.session_id)

    def end_session(self) -> None:
        """Quit the Appium session."""
        if self._driver:
            try:
                self._driver.quit()
                logger.info("Appium session ended", device_id=self._device_id)
            except Exception as exc:
                logger.warning("Error ending Appium session", error=str(exc))
            finally:
                self._driver = None

    @property
    def driver(self) -> webdriver.Remote:
        if self._driver is None:
            raise RuntimeError("Appium session not started. Use as context manager or call start_session().")
        return self._driver

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
        """Tap at absolute screen coordinates."""
        logger.debug("tap", x=x, y=y, device_id=self._device_id)
        self._retry(lambda: self.driver.execute_script(
            "mobile: clickGesture",
            {"x": x, "y": y, "duration": duration_ms},
        ))

    def long_press(self, x: int, y: int, duration_ms: int = 1500) -> None:
        """Long-press at absolute screen coordinates."""
        logger.debug("long_press", x=x, y=y, duration_ms=duration_ms)
        self._retry(lambda: self.driver.execute_script(
            "mobile: longClickGesture",
            {"x": x, "y": y, "duration": duration_ms},
        ))

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> None:
        """Swipe from (start_x, start_y) to (end_x, end_y)."""
        logger.debug("swipe", start=(start_x, start_y), end=(end_x, end_y))
        self._retry(lambda: self.driver.execute_script(
            "mobile: swipeGesture",
            {
                "startX": start_x,
                "startY": start_y,
                "endX": end_x,
                "endY": end_y,
                "duration": duration_ms,
            },
        ))

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
        logger.debug("press_back", device_id=self._device_id)
        self._retry(lambda: self.driver.press_keycode(4))

    def press_home(self) -> None:
        """Press the Android Home button."""
        logger.debug("press_home", device_id=self._device_id)
        self._retry(lambda: self.driver.press_keycode(3))

    def press_key(self, keycode: int) -> None:
        """Press an arbitrary Android keycode."""
        self._retry(lambda: self.driver.press_keycode(keycode))

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
        logger.debug("type_text", length=len(text))
        if clear_first:
            active = self._retry(lambda: self.driver.switch_to.active_element)
            self._retry(lambda: active.clear())
        self._retry(lambda: self.driver.execute_script(
            "mobile: type", {"text": text}
        ))

    def type_into_element(self, element: WebElement, text: str, clear_first: bool = True) -> None:
        """Type text into a specific WebElement."""
        if clear_first:
            self._retry(lambda: element.clear())
        self._retry(lambda: element.send_keys(text))

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

    # ------------------------------------------------------------------
    # App management
    # ------------------------------------------------------------------

    def install_app(self, apk_path: str) -> None:
        """
        Install an APK on the device.

        Args:
            apk_path: Local path to the APK file.
        """
        logger.info("install_app", apk=apk_path)
        self._retry(lambda: self.driver.install_app(apk_path))

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
        raise last_exc  # type: ignore[misc]
