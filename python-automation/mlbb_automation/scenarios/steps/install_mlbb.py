"""
Step: Install Mobile Legends: Bang Bang from Google Play Store.

Flow (default — open_via_play_store=True):
  1. Always open Play Store MLBB page (even if already installed)
  2. If "Install" is visible → install, wait, then tap "Play"/"Open"
  3. If "Play"/"Open"/"Играть" is already visible → tap it directly
  4. Wait for MLBB loading screen to confirm launch

Flow (open_via_play_store=False, legacy):
  1. If MLBB is already installed → skip install
  2. Otherwise open Play Store → Install → wait → Open
  3. Wait for MLBB loading screen
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
# "Play" / "Играть" appear on Play Store when the app is already installed and ready to launch
_OPEN_LABELS = ("Play", "Open", "Играть", "Открыть", "Запустить")

# OCR signals for state detection (lowercase)
_INSTALL_SIGNALS = ("install", "установить")
_INSTALLING_SIGNALS = ("installing", "downloading", "загрузка", "установка", "pending")
_OPEN_SIGNALS = ("play", "open", "играть", "открыть", "запустить")
_ALREADY_INSTALLED_SIGNALS = ("play", "open", "uninstall", "update", "играть", "открыть", "удалить")

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
    open_via_play_store: bool = True,
) -> None:
    """
    Open Google Play Store and install / launch MLBB.

    Args:
        executor:             Active AppiumExecutor session.
        run_logger:           RunLogger for this automation run.
        device_id:            Device ID for log context.
        open_via_play_store:  When True (default), always navigate to the Play
                              Store MLBB page and tap "Play"/"Играть".
                              This is correct for the simplified scenario where
                              the app is already installed.
                              When False (legacy), skip Play Store if the app is
                              already installed and launch directly by package.
    """
    run_logger.log_step("install_mlbb", "started", device_id=device_id)
    logger.info("install_mlbb starting", device_id=device_id, open_via_play_store=open_via_play_store)

    if open_via_play_store:
        # Always open Play Store — works whether app is installed or not
        _open_play_store(executor, run_logger, device_id)

        # Check what Play Store is showing
        from ...cv.ocr import OcrEngine
        ocr = OcrEngine()
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        already_ready = any(s in texts for s in _OPEN_SIGNALS)
        needs_install = any(s in texts for s in _INSTALL_SIGNALS) and not already_ready

        if needs_install:
            logger.info("Play Store shows Install — installing now", device_id=device_id)
            _tap_install_and_wait(executor, run_logger, device_id)

        # Tap Play / Open / Играть
        _launch_from_play_store(executor, run_logger, device_id)

    else:
        # Legacy: skip Play Store if already installed
        if executor.is_app_installed(MLBB_PACKAGE):
            logger.info("MLBB already installed — skipping install", device_id=device_id)
            run_logger.log_step("install_mlbb", "already_installed", device_id=device_id)
        else:
            _open_play_store(executor, run_logger, device_id)
            _tap_install_and_wait(executor, run_logger, device_id)

        _launch_mlbb(executor, run_logger, device_id)

    # Wait for the loading screen
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


def _launch_from_play_store(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Tap "Play" / "Играть" / "Open" / "Открыть" on the Play Store MLBB page.

    Used in the simplified scenario when the app is pre-installed.
    Tries UiAutomator2 element search first; falls back to ADB subprocess tap
    at known coordinates if the element search fails.
    """
    logger.info("Tapping Play/Open on Play Store", device_id=device_id)
    run_logger.log_step("install_mlbb", "tapping_play", device_id=device_id)

    # Stage A: UiAutomator2 element search
    for label in _OPEN_LABELS:
        try:
            x, y = executor.find_element(label, retries=3)
            executor.tap(x, y)
            logger.info("Tapped Play/Open via UiAutomator2", label=label, device_id=device_id)
            time.sleep(3)
            img = executor.screenshot()
            run_logger.save_screenshot(img, label="play_tapped")
            return
        except RuntimeError:
            continue

    # Stage B: OCR — find the button position from text bounding boxes
    logger.info("UiAutomator2 failed — trying OCR tap", device_id=device_id)
    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()
    img = executor.screenshot()
    results = ocr.read_region(img)
    for result in results:
        if any(s in result.text.lower() for s in _OPEN_SIGNALS):
            logger.info("Found Play/Open via OCR", text=result.text, device_id=device_id)
            executor.tap(result.cx, result.cy)
            time.sleep(3)
            img = executor.screenshot()
            run_logger.save_screenshot(img, label="play_tapped_ocr")
            return

    # Stage C: ADB subprocess tap — Play Store "Играть" button is at roughly
    # center-top on Samsung Galaxy A13 (1080×2408) — try center of screen,
    # upper half where CTA buttons typically live.
    logger.warning(
        "OCR tap failed — falling back to ADB tap at heuristic coordinates",
        device_id=device_id,
    )
    import subprocess
    try:
        size = executor.get_screen_size()
        cx = size[0] // 2       # horizontal center
        cy = int(size[1] * 0.28)  # ~28% from top — Play Store CTA button zone
        logger.info("ADB tap at heuristic coords", x=cx, y=cy, device_id=device_id)
        subprocess.run(["adb", "shell", "input", "tap", str(cx), str(cy)], timeout=10)
        time.sleep(3)
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="play_tapped_adb")
        return
    except Exception as exc:
        logger.error("ADB tap failed", error=str(exc), device_id=device_id)

    raise StepError(
        "Could not tap Play/Open button on Play Store in any supported language or fallback method"
    )


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
        # Russian signals from real Samsung Galaxy A13 screenshots
        _fast_menu_signals = (
            "classic", "profile", "shop", "battle",
            "подготовка", "герои", "сумка", "магазин", "обычный",
        )
        if any(s in texts for s in _fast_menu_signals):
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
