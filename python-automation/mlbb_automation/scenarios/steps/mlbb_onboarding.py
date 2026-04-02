"""
Step: Launch MLBB, skip onboarding, close popups, reach main menu.

Flow:
  1. Wait for MLBB loading screen to complete
  2. Handle first-launch screens: server selection, intro video, tutorial
  3. Dismiss recurring start-up popups (events, banners, notifications)
  4. Confirm we are on the main menu (OCR: "Classic" or "Ranked" visible)

MLBB displays different onboarding flows depending on:
  - Whether it's a fresh install vs. returning player
  - Server region
  - Current in-game events

The step uses OCR to identify each screen and taps the appropriate button.
All taps go through executor.find_element() (3-stage: template → OCR → Appium).
"""

from __future__ import annotations

import time
from typing import List

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

# Time constants
_LOADING_TIMEOUT = 300   # MLBB can take up to 5 min to load on first launch
_MAIN_MENU_TIMEOUT = 120
_POLL_INTERVAL = 3.0

# OCR text that indicates we have reached the main menu
_MAIN_MENU_SIGNALS = ("classic", "ranked", "brawl", "shop", "profile", "battle")

# Screens to tap-through during onboarding
_TAP_THROUGH_BUTTONS = (
    "tap to continue",
    "tap to skip",
    "skip",
    "next",
    "start",
    "ok",
    "confirm",
    "close",
    "got it",
    "claim",
    "collect",
    "continue",
    "no thanks",
    "later",
    "not now",
)

# Danger words — never tap these during onboarding (they're purchase CTAs)
_GUARD_WORDS = ("buy", "purchase", "recharge", "top up", "diamonds", "pay")

# Server selection screen signals
_SERVER_SELECT_SIGNALS = ("select server", "choose server", "server")


class StepError(Exception):
    """Non-recoverable error in a step."""


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str = "",
) -> None:
    """
    Launch MLBB and navigate to the main menu past all onboarding screens.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        device_id:  Device ID for log context.
    """
    run_logger.log_step("mlbb_onboarding", "started", device_id=device_id)
    logger.info("mlbb_onboarding starting", device_id=device_id)

    # Ensure MLBB is in the foreground
    from ...scenarios.steps.install_mlbb import MLBB_PACKAGE
    executor.launch_app(MLBB_PACKAGE)
    time.sleep(3)

    # Wait for the game to finish its initial loading phase
    _wait_for_loading(executor, run_logger, device_id)

    # Navigate through onboarding and popups to the main menu
    _navigate_to_main_menu(executor, run_logger, device_id)

    run_logger.log_step("mlbb_onboarding", "ok", device_id=device_id)
    logger.info("mlbb_onboarding completed — main menu reached", device_id=device_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _wait_for_loading(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Wait until MLBB finishes the initial loading/patch download phase.

    The loading screen has a progress bar and the Moonton/MLBB logo.
    We poll until loading signals disappear or main menu signals appear.
    """
    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    logger.info("Waiting for MLBB initial load to complete", device_id=device_id)
    _loading_signals = ("loading", "moonton", "downloading", "updating", "patch")

    deadline = time.monotonic() + _LOADING_TIMEOUT
    last_screenshot_time = 0.0

    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        # Main menu reached — loading complete
        if any(s in texts for s in _MAIN_MENU_SIGNALS):
            logger.info("Loading complete — main menu detected", device_id=device_id)
            run_logger.save_screenshot(img, label="loading_complete")
            return

        # Log a screenshot every 60 seconds during long loads
        now = time.monotonic()
        if now - last_screenshot_time >= 60:
            run_logger.save_screenshot(img, label="loading_progress")
            last_screenshot_time = now

        # Any interactive screen that isn't loading — handle it
        if not any(s in texts for s in _loading_signals):
            # May be a tap-to-continue intro screen
            _try_tap_through(executor, ocr, img, device_id)

        time.sleep(_POLL_INTERVAL)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="loading_timeout")
    raise StepError(
        f"MLBB failed to complete initial loading within {_LOADING_TIMEOUT}s"
    )


def _navigate_to_main_menu(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Dismiss onboarding screens and popups until the main menu is visible.

    Strategy: poll the screen; if a known skip/confirm button is visible,
    tap it.  If the screen hasn't changed for 10s, try a tap in the center
    (some intro videos require a tap anywhere to skip).
    """
    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    logger.info("Navigating through onboarding to main menu", device_id=device_id)

    deadline = time.monotonic() + _MAIN_MENU_TIMEOUT
    last_action_time = time.monotonic()
    dismissed = 0

    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        # ── Main menu reached ──────────────────────────────────────────
        if any(s in texts for s in _MAIN_MENU_SIGNALS):
            logger.info(
                "Main menu reached",
                dismissed_popups=dismissed,
                device_id=device_id,
            )
            run_logger.save_screenshot(img, label="main_menu_reached")
            return

        # ── Server selection ───────────────────────────────────────────
        if any(s in texts for s in _SERVER_SELECT_SIGNALS):
            _select_server(executor, ocr, img, run_logger, device_id)
            last_action_time = time.monotonic()
            dismissed += 1
            time.sleep(2)
            continue

        # ── Tap-through buttons ────────────────────────────────────────
        tapped = _try_tap_through(executor, ocr, img, device_id)
        if tapped:
            dismissed += 1
            last_action_time = time.monotonic()
            time.sleep(1.5)
            continue

        # ── No known button — try a center tap after 10s of inactivity ─
        if time.monotonic() - last_action_time >= 10:
            logger.info(
                "No button found — tapping screen center",
                device_id=device_id,
            )
            size = executor.get_screen_size()
            executor.tap(size[0] // 2, size[1] // 2)
            last_action_time = time.monotonic()

        time.sleep(_POLL_INTERVAL)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="onboarding_timeout")
    raise StepError(
        f"Could not reach MLBB main menu within {_MAIN_MENU_TIMEOUT}s of onboarding"
    )


def _try_tap_through(executor, ocr, img, device_id: str) -> bool:
    """
    Scan OCR results for a known skip/confirm button and tap it.

    Returns True if a button was tapped, False otherwise.
    """
    results = ocr.read_region(img)

    for result in results:
        word = result.text.lower().strip(".,!?:")
        if result.confidence < 0.5:
            continue
        if word in _GUARD_WORDS:
            continue
        if word in _TAP_THROUGH_BUTTONS or any(btn in word for btn in _TAP_THROUGH_BUTTONS):
            executor.tap(result.cx, result.cy)
            logger.info("Tapped onboarding button", button=result.text, device_id=device_id)
            return True
    return False


def _select_server(executor, ocr, img, run_logger, device_id: str) -> None:
    """
    Handle server selection screen.

    Tries to select the first available server option (usually the
    recommended/default option at the top of the list).
    """
    logger.info("Server selection screen detected", device_id=device_id)
    run_logger.save_screenshot(img, label="server_selection")

    # Prefer server options labeled as recommended/default
    _preferred = ("recommended", "na", "us", "america", "europe", "asia")
    results = ocr.read_region(img)
    for result in results:
        if any(pref in result.text.lower() for pref in _preferred):
            executor.tap(result.cx, result.cy)
            time.sleep(1)
            break
    else:
        # No recognized server name — tap the topmost item in list
        # (approximate: upper-third of screen, center-x)
        size = executor.get_screen_size()
        executor.tap(size[0] // 2, size[1] // 3)
        time.sleep(1)

    # Tap Confirm / OK to accept the selection
    try:
        x, y = executor.find_element("Confirm", retries=2)
        executor.tap(x, y)
    except RuntimeError:
        try:
            x, y = executor.find_element("OK", retries=2)
            executor.tap(x, y)
        except RuntimeError:
            pass
