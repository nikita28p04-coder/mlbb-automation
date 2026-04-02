"""
Recovery manager — detects hangs and attempts to restore the automation session.

Strategy:
  1. Screenshot is taken periodically; if it doesn't change within `freeze_timeout`
     seconds, the device is considered hung.
  2. Recovery steps are attempted in order:
       a. Press Back (dismiss dialogs)
       b. Press Home (go to launcher)
       c. Relaunch the target app
       d. If still hung after max_recovery_attempts → raise RecoveryError

Usage:
    manager = RecoveryManager(executor, app_package="com.mobile.legends")
    manager.start_watchdog()          # background thread
    # ... run automation ...
    manager.stop_watchdog()

    # Or call manually:
    manager.attempt_recovery("payment_step")
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from typing import Optional

from PIL import Image

from ..actions.executor import AppiumExecutor
from ..logging.logger import get_logger

logger = get_logger(__name__)


class RecoveryError(Exception):
    """Raised when all recovery attempts have been exhausted."""


class RecoveryManager:
    """
    Monitors the device for hangs and attempts automatic recovery.

    Args:
        executor:              The AppiumExecutor for the active session.
        app_package:           Android package to relaunch on recovery.
        freeze_timeout:        Seconds without screen change before recovery.
        max_recovery_attempts: How many recovery attempts before giving up.
        check_interval:        How often (seconds) to poll for screen changes.
    """

    def __init__(
        self,
        executor: AppiumExecutor,
        app_package: str,
        freeze_timeout: int = 30,
        max_recovery_attempts: int = 3,
        check_interval: int = 5,
    ) -> None:
        self._executor = executor
        self._app_package = app_package
        self._freeze_timeout = freeze_timeout
        self._max_recovery_attempts = max_recovery_attempts
        self._check_interval = check_interval

        self._last_screen_hash: Optional[str] = None
        self._last_change_time: float = time.time()
        self._recovery_count: int = 0

        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Watchdog thread
    # ------------------------------------------------------------------

    def start_watchdog(self) -> None:
        """Start the background watchdog thread."""
        self._stop_event.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="RecoveryWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        logger.info(
            "Recovery watchdog started",
            freeze_timeout=self._freeze_timeout,
            check_interval=self._check_interval,
        )

    def stop_watchdog(self) -> None:
        """Signal the watchdog thread to stop and wait for it."""
        self._stop_event.set()
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=10)
        logger.info("Recovery watchdog stopped")

    def _watchdog_loop(self) -> None:
        """Main loop: poll screenshots, detect freeze, trigger recovery."""
        while not self._stop_event.is_set():
            try:
                self._check_for_freeze()
            except RecoveryError:
                logger.error("Watchdog: max recovery attempts exhausted")
                break
            except Exception as exc:
                logger.warning("Watchdog error (non-fatal)", error=str(exc))
            time.sleep(self._check_interval)

    def _check_for_freeze(self) -> None:
        img = self._executor.screenshot()
        current_hash = self._image_hash(img)

        if current_hash != self._last_screen_hash:
            # Screen changed — device is alive
            self._last_screen_hash = current_hash
            self._last_change_time = time.time()
            self._recovery_count = 0
            return

        frozen_for = time.time() - self._last_change_time
        if frozen_for >= self._freeze_timeout:
            logger.warning(
                "Device appears frozen",
                frozen_for_seconds=round(frozen_for),
            )
            self.attempt_recovery("watchdog_freeze")

    # ------------------------------------------------------------------
    # Manual recovery
    # ------------------------------------------------------------------

    def attempt_recovery(self, context: str = "manual") -> None:
        """
        Attempt to recover from a hung state.

        Args:
            context: Human-readable label for what triggered recovery.

        Raises:
            RecoveryError: If all recovery attempts are exhausted.
        """
        self._recovery_count += 1
        if self._recovery_count > self._max_recovery_attempts:
            raise RecoveryError(
                f"Recovery failed after {self._max_recovery_attempts} attempts "
                f"(context: {context})"
            )

        logger.warning(
            "Attempting recovery",
            attempt=self._recovery_count,
            max_attempts=self._max_recovery_attempts,
            context=context,
        )

        steps = [
            ("press_back", self._recover_press_back),
            ("press_home", self._recover_press_home),
            ("relaunch_app", self._recover_relaunch_app),
        ]

        for step_name, step_fn in steps:
            try:
                logger.info("Recovery step", step=step_name)
                step_fn()
                time.sleep(3)

                # Check if screen changed after recovery
                img = self._executor.screenshot()
                new_hash = self._image_hash(img)
                if new_hash != self._last_screen_hash:
                    logger.info("Recovery succeeded", step=step_name)
                    self._last_screen_hash = new_hash
                    self._last_change_time = time.time()
                    return
            except Exception as exc:
                logger.warning("Recovery step failed", step=step_name, error=str(exc))

        logger.error("All recovery steps failed for this attempt")

    # ------------------------------------------------------------------
    # Recovery steps
    # ------------------------------------------------------------------

    def _recover_press_back(self) -> None:
        """Try pressing Back to dismiss a dialog or return to previous screen."""
        self._executor.press_back()

    def _recover_press_home(self) -> None:
        """Press Home to go back to the Android launcher."""
        self._executor.press_home()
        time.sleep(2)

    def _recover_relaunch_app(self) -> None:
        """Force-stop and relaunch the target application."""
        try:
            self._executor.stop_app(self._app_package)
        except Exception:
            pass
        time.sleep(2)
        self._executor.launch_app(self._app_package)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _image_hash(img: Image.Image) -> str:
        """
        Compute a fast perceptual hash of a screenshot for change detection.

        Resize to 32×32 grayscale and MD5 the raw bytes — good enough to
        detect meaningful screen changes while ignoring minor rendering artifacts.
        """
        small = img.resize((32, 32)).convert("L")
        return hashlib.md5(small.tobytes()).hexdigest()

    def notify_action(self) -> None:
        """
        Call this after any successful user action to reset the freeze timer.

        Prevents false-positive freeze detection during slow but progressing steps.
        """
        self._last_change_time = time.time()
        try:
            img = self._executor.screenshot()
            self._last_screen_hash = self._image_hash(img)
        except Exception:
            pass
