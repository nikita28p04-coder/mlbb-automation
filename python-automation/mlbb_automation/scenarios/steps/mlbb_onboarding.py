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

import subprocess
import time
from typing import List, Optional

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

# Time constants
_LOADING_TIMEOUT = 300   # MLBB can take up to 5 min to load on first launch
_MAIN_MENU_TIMEOUT = 120
_POLL_INTERVAL = 3.0

# OCR text that indicates we have reached the main menu.
# Includes both English and Russian (device locale) variants confirmed from real screenshots.
_MAIN_MENU_SIGNALS = (
    # English
    "classic", "ranked", "brawl", "shop", "profile", "battle",
    # Russian — from real Samsung Galaxy A13 screenshots
    "подготовка", "герои", "сумка", "обычный", "магазин",
)

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

def _adb_foreground_pkg(adb_serial: str) -> str:
    """
    Return the package name currently in foreground via ADB.
    Uses ``dumpsys activity activities`` — fast, no Appium needed.
    Uses process-group kill so the call never hangs beyond the timeout.
    Returns empty string on any failure.
    """
    import os, signal
    cmd = ["adb", "-s", adb_serial, "shell", "dumpsys", "activity", "activities"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        try:
            stdout, _ = proc.communicate(timeout=10)
            text = stdout.decode(errors="replace")
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
            return ""
    except Exception:
        return ""

    for line in text.splitlines():
        if "mResumedActivity" in line or "ResumedActivity" in line:
            for part in line.strip().split():
                if "/" in part and "." in part:
                    return part.split("/")[0]
    return ""


def _wait_for_loading(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Wait until MLBB finishes its initial loading/patch download phase.

    Strategy (fast path):
      1. Poll ADB every 5 s to check MLBB is in foreground (no OCR cost).
      2. Once MLBB is stable in foreground, take one screenshot and OCR only
         the BOTTOM 40 % of the screen — that is where «Classic», «Ranked»,
         «подготовка», etc. appear on the main menu.
      3. If main-menu signals are found → done.
      4. If not, tap any intro/skip button visible in that strip, then wait
         another 10 s before the next OCR pass.

    This avoids running full-screen OCR (which takes 3+ min on CPU) in a tight
    polling loop.
    """
    from ...cv.ocr import OcrEngine
    from ...scenarios.steps.install_mlbb import MLBB_PACKAGE

    ocr = OcrEngine()
    adb_serial: Optional[str] = getattr(executor, "_adb_serial", None)

    logger.info("Waiting for MLBB initial load to complete", device_id=device_id)

    deadline = time.monotonic() + _LOADING_TIMEOUT
    last_ocr_time = 0.0          # time of last OCR pass
    last_progress_shot = 0.0     # time of last progress screenshot
    _OCR_INTERVAL = 12.0         # seconds between OCR passes
    _ADB_INTERVAL = 5.0          # seconds between ADB foreground checks

    while time.monotonic() < deadline:
        # ── 1. ADB foreground check (free / fast) ─────────────────────────
        if adb_serial:
            fg = _adb_foreground_pkg(adb_serial)
            if fg and fg != MLBB_PACKAGE:
                logger.info(
                    "Waiting for MLBB foreground",
                    current_fg=fg,
                    device_id=device_id,
                )
                time.sleep(_ADB_INTERVAL)
                continue

        # ── 2. Throttle OCR passes ────────────────────────────────────────
        now = time.monotonic()
        if now - last_ocr_time < _OCR_INTERVAL:
            time.sleep(2)
            continue
        last_ocr_time = now

        # ── 3. Screenshot + cropped OCR (bottom 40 % only) ───────────────
        img = executor.screenshot()
        w, h = img.size
        crop_top = int(h * 0.60)
        bottom_strip = img.crop((0, crop_top, w, h))
        results = ocr.read_region(bottom_strip)
        texts = " ".join(r.text.lower() for r in results)

        # Main menu reached
        if any(s in texts for s in _MAIN_MENU_SIGNALS):
            logger.info("Loading complete — main menu detected", device_id=device_id)
            run_logger.save_screenshot(img, label="loading_complete")
            return

        # Progress screenshot every 60 s
        if now - last_progress_shot >= 60:
            run_logger.save_screenshot(img, label="loading_progress")
            last_progress_shot = now

        logger.info(
            "MLBB still loading",
            bottom_texts=texts[:120],
            device_id=device_id,
        )

        # Tap-through any intro/skip button visible in the bottom strip
        # Pass pre-computed results to avoid a second OCR call
        _try_tap_through_results(executor, results, crop_offset_y=crop_top,
                                 device_id=device_id)

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

        # ── Tap-through buttons (reuse already-computed results) ──────
        tapped = _try_tap_through_results(executor, results, device_id=device_id)
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


def _try_tap_through_results(
    executor,
    results: list,
    crop_offset_y: int = 0,
    device_id: str = "",
) -> bool:
    """
    Scan pre-computed OCR results for a skip/confirm button and tap it.

    ``crop_offset_y`` adjusts the y coordinate when results were computed on a
    cropped image (the tap must target the full-screen coordinate).

    Returns True if a button was tapped, False otherwise.
    """
    for result in results:
        word = result.text.lower().strip(".,!?:")
        if result.confidence < 0.5:
            continue
        if word in _GUARD_WORDS:
            continue
        if word in _TAP_THROUGH_BUTTONS or any(btn in word for btn in _TAP_THROUGH_BUTTONS):
            executor.tap(result.cx, result.cy + crop_offset_y)
            logger.info("Tapped onboarding button", button=result.text, device_id=device_id)
            return True
    return False


def _try_tap_through(executor, ocr, img, device_id: str) -> bool:
    """
    Scan the full image via OCR for a skip/confirm button and tap it.

    Kept for callers that have not yet been migrated to pass pre-computed
    results.  Prefer ``_try_tap_through_results`` to avoid a redundant OCR
    pass.

    Returns True if a button was tapped, False otherwise.
    """
    results = ocr.read_region(img)
    return _try_tap_through_results(executor, results, device_id=device_id)


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
