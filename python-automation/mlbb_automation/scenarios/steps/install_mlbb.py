"""
Step: Install Mobile Legends: Bang Bang from Google Play Store.

Flow:
  1. If MLBB is already installed → skip (idempotent)
  2. Open Play Store via market:// intent
  3. Tap "Install" button and wait for download + install to complete
  4. Tap "Open" when it appears, OR launch app by package name
  5. Wait for MLBB loading screen to confirm launch
"""

from __future__ import annotations

import time

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

MLBB_PACKAGE = "com.mobile.legends"
PLAY_STORE_PACKAGE = "com.android.vending"

# UI text labels — tried in order during find_element calls (EN then RU)
_INSTALL_LABELS = ("Install", "Установить")
_OPEN_LABELS = ("Open", "Открыть")

# OCR signals for state detection (lowercase)
_INSTALL_SIGNALS = ("install", "установить")
_INSTALLING_SIGNALS = ("installing", "downloading", "загрузка", "установка", "pending")
_OPEN_SIGNALS = ("open", "открыть")
_ALREADY_INSTALLED_SIGNALS = ("open", "uninstall", "update", "открыть", "удалить")

# Timeouts
_INSTALL_TIMEOUT = 600  # 10 minutes — MLBB is a large download
_POLL_INTERVAL = 5.0
_LAUNCH_TIMEOUT = 60


class StepError(Exception):
    """Non-recoverable error in a step."""


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str = "",
) -> None:
    """
    Open Google Play Store and install MLBB.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        device_id:  Device ID for log context.
    """
    run_logger.log_step("install_mlbb", "started", device_id=device_id)
    logger.info("install_mlbb starting", device_id=device_id)

    # Step 1: Check if already installed
    if executor.is_app_installed(MLBB_PACKAGE):
        logger.info("MLBB already installed — skipping install", device_id=device_id)
        run_logger.log_step("install_mlbb", "already_installed", device_id=device_id)
    else:
        # Step 2: Open Play Store via market:// intent
        _open_play_store(executor, run_logger, device_id)

        # Step 3: Tap Install and wait
        _tap_install_and_wait(executor, run_logger, device_id)

    # Step 4: Launch MLBB (tap Open or start by package)
    _launch_mlbb(executor, run_logger, device_id)

    # Step 5: Wait for the loading screen
    _wait_for_mlbb_loading(executor, run_logger, device_id)

    run_logger.log_step("install_mlbb", "ok", device_id=device_id)
    logger.info("install_mlbb completed", device_id=device_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _open_play_store(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Open the Play Store MLBB page via market:// intent."""
    logger.info("Opening Play Store for MLBB", device_id=device_id)
    run_logger.log_step("install_mlbb", "open_play_store", device_id=device_id)

    executor.driver.execute_script("mobile: startActivity", {
        "intent": f"market://details?id={MLBB_PACKAGE}",
    })
    time.sleep(4)  # Play Store takes a moment to load

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="play_store_mlbb")


def _tap_install_and_wait(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Tap the Install button and wait for installation to complete."""
    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    # Check if already showing "Open" (already installed) or "Install"
    img = executor.screenshot()
    results = ocr.read_region(img)
    texts = " ".join(r.text.lower() for r in results)

    already_installed = (
        any(s in texts for s in _ALREADY_INSTALLED_SIGNALS)
        and not any(s in texts for s in _INSTALL_SIGNALS)
    )
    if already_installed:
        logger.info("Play Store shows Open/Uninstall — app already installed", device_id=device_id)
        run_logger.log_step("install_mlbb", "play_store_already_installed", device_id=device_id)
        return

    # Tap Install — try each localized label in turn
    logger.info("Tapping Install", device_id=device_id)
    run_logger.log_step("install_mlbb", "tapping_install", device_id=device_id)
    installed = False
    for label in _INSTALL_LABELS:
        try:
            x, y = executor.find_element(label, template_name=None, retries=2)
            executor.tap(x, y)
            installed = True
            break
        except RuntimeError:
            continue
    if not installed:
        raise StepError("Could not find Install button on Play Store in any supported language")

    time.sleep(2)
    img = executor.screenshot()
    run_logger.save_screenshot(img, label="install_tapped")

    # Wait for download + installation to finish
    logger.info("Waiting for MLBB download and installation", device_id=device_id)
    deadline = time.monotonic() + _INSTALL_TIMEOUT
    last_log = time.monotonic()

    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        # "Open" button means installation completed
        if any(btn in texts for btn in _OPEN_SIGNALS):
            logger.info("Installation completed — Open button visible", device_id=device_id)
            run_logger.save_screenshot(img, label="install_complete")
            run_logger.log_step("install_mlbb", "install_complete", device_id=device_id)
            return

        # Still downloading/installing — log progress periodically
        if time.monotonic() - last_log >= 30:
            run_logger.save_screenshot(img, label="install_progress")
            logger.info("Installation in progress...", device_id=device_id)
            last_log = time.monotonic()

        time.sleep(_POLL_INTERVAL)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="install_timeout")
    raise StepError(
        f"MLBB installation timed out after {_INSTALL_TIMEOUT}s. "
        "Check Play Store for errors."
    )


def _launch_mlbb(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Tap 'Open' on Play Store or launch MLBB by package name."""
    logger.info("Launching MLBB", device_id=device_id)

    # First try tapping "Open" in all supported languages if visible on Play Store
    for label in _OPEN_LABELS:
        try:
            x, y = executor.find_element(label, retries=2)
            executor.tap(x, y)
            logger.info("Tapped Open on Play Store", label=label, device_id=device_id)
            time.sleep(3)
            return
        except RuntimeError:
            continue

    # Fall back to direct package launch
    logger.info("Launching MLBB by package name", device_id=device_id)
    executor.launch_app(MLBB_PACKAGE)
    time.sleep(3)


def _wait_for_mlbb_loading(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Wait until the MLBB loading screen is visible."""
    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    logger.info("Waiting for MLBB to load", device_id=device_id)
    _loading_signals = ("loading", "mobile legends", "moonton", "загрузка")

    deadline = time.monotonic() + _LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        if any(s in texts for s in _loading_signals):
            logger.info("MLBB loading screen detected", device_id=device_id)
            run_logger.save_screenshot(img, label="mlbb_loading")
            return

        # Also check if we're already at main menu (fast device)
        if "classic" in texts or "profile" in texts or "shop" in texts:
            logger.info("MLBB already at main menu", device_id=device_id)
            run_logger.save_screenshot(img, label="mlbb_main_menu_fast")
            return

        time.sleep(2)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="mlbb_launch_timeout")
    # Non-fatal: the game may still be loading
    logger.warning(
        "MLBB loading screen not detected within timeout — proceeding anyway",
        device_id=device_id,
    )
