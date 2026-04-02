"""
Step: Add Google account to the Android device.

Flow:
  1. Open Android Settings via intent
  2. Navigate to Accounts → Add Account → Google
  3. Enter email address → Next
  4. Enter password → Next
  5. Handle intermediate screens (Terms/Privacy, Skip backup, No thanks for payment)
  6. Verify account was added

The account has NO 2FA — if a 2FA screen appears, raise StepError so the
caller can retry or abort.
"""

from __future__ import annotations

import time
from typing import Optional

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

SETTINGS_PACKAGE = "com.android.settings"
SETTINGS_ACCOUNTS_ACTIVITY = "com.android.settings.accounts.AccountSettings"

# Text fragments searched via OCR / UI hierarchy — tolerant of minor UI variations
_EMAIL_PROMPTS = ("enter your email", "sign in", "add your email")
_PASSWORD_PROMPTS = ("enter your password", "password")
_INTERMEDIATE_SKIP = (
    "skip",
    "no thanks",
    "not now",
    "later",
    "decline",
    "i agree",
    "accept",
    "agree",
    "continue",
    "next",
    "done",
    "got it",
    "ok",
)
_SUCCESS_SIGNALS = ("account added", "sync", "google account", "gmail")
_2FA_SIGNALS = ("verify", "2-step", "two-step", "verification code", "authenticator")

# Max seconds to wait for each major state transition
_TRANSITION_TIMEOUT = 60
_POLL_INTERVAL = 2.0


class StepError(Exception):
    """Non-recoverable error in a step."""


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    email: str,
    password: str,
    device_id: str = "",
) -> None:
    """
    Add a Google account to the device via Android Settings.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        email:      Google account email.
        password:   Google account password (no 2FA).
        device_id:  Device ID for log context.
    """
    run_logger.log_step("google_account", "started", device_id=device_id)
    logger.info("google_account starting", device_id=device_id, email=email)

    # Step 1: Open Android Settings directly to accounts section
    _open_settings_accounts(executor, run_logger, device_id)

    # Step 2: Tap "Add Account" → choose "Google"
    _tap_add_account(executor, run_logger, device_id)

    # Step 3: Enter email address
    _enter_email(executor, run_logger, email, device_id)

    # Step 4: Enter password
    _enter_password(executor, run_logger, password, device_id)

    # Step 5: Handle intermediate screens (Terms, Backup, etc.)
    _handle_intermediate_screens(executor, run_logger, device_id)

    # Step 6: Verify account added
    _verify_account_added(executor, run_logger, email, device_id)

    run_logger.log_step("google_account", "ok", device_id=device_id)
    logger.info("google_account completed", device_id=device_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _open_settings_accounts(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Open Android Settings → Accounts screen via intent."""
    logger.info("Opening Settings → Accounts", device_id=device_id)
    run_logger.log_step("google_account", "open_settings", device_id=device_id)

    # Use adb-style intent through Appium to jump straight to accounts settings
    executor.driver.execute_script("mobile: startActivity", {
        "intent": "android.settings.SYNC_SETTINGS",
    })
    time.sleep(2)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="settings_accounts")


def _tap_add_account(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Tap 'Add Account' then select 'Google'."""
    logger.info("Tapping Add Account", device_id=device_id)

    # Find and tap "Add account" button
    x, y = executor.find_element("Add account", retries=3)
    executor.tap(x, y)
    time.sleep(1)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="add_account_list")

    # Select "Google" from the account type list
    x, y = executor.find_element("Google", retries=3)
    executor.tap(x, y)
    time.sleep(2)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="google_account_type")


def _enter_email(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    email: str,
    device_id: str,
) -> None:
    """Wait for the email entry screen and type the email address."""
    logger.info("Entering email address", device_id=device_id)

    # Wait for the email field to appear
    deadline = time.monotonic() + _TRANSITION_TIMEOUT
    while time.monotonic() < deadline:
        img = executor.screenshot()
        from ...cv.ocr import OcrEngine
        ocr = OcrEngine()
        results = ocr.read_region(img)
        texts = [r.text.lower() for r in results]
        if any(prompt in " ".join(texts) for prompt in _EMAIL_PROMPTS):
            break
        time.sleep(_POLL_INTERVAL)
    else:
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="email_screen_timeout")
        raise StepError("Timed out waiting for Google email entry screen")

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="email_entry_screen")

    # Tap the email field and type
    try:
        x, y = executor.find_element("Enter your email", retries=2)
        executor.tap(x, y)
    except RuntimeError:
        # Field might already be focused
        pass

    time.sleep(0.5)
    executor.type_text(email)
    time.sleep(0.5)

    # Tap "Next"
    try:
        x, y = executor.find_element("Next", retries=2)
        executor.tap(x, y)
    except RuntimeError:
        executor.press_key(66)  # KEYCODE_ENTER
    time.sleep(2)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="after_email_next")


def _enter_password(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    password: str,
    device_id: str,
) -> None:
    """Wait for the password screen and type the password."""
    logger.info("Entering password", device_id=device_id)

    # Wait for password field
    deadline = time.monotonic() + _TRANSITION_TIMEOUT
    while time.monotonic() < deadline:
        img = executor.screenshot()
        from ...cv.ocr import OcrEngine
        ocr = OcrEngine()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        # Check for 2FA — should not occur but raise explicitly if it does
        if any(s in texts for s in _2FA_SIGNALS):
            run_logger.save_screenshot(img, label="unexpected_2fa_screen")
            raise StepError(
                "2FA screen appeared unexpectedly. "
                "This Google account appears to have 2FA enabled."
            )

        if any(prompt in texts for prompt in _PASSWORD_PROMPTS):
            break
        time.sleep(_POLL_INTERVAL)
    else:
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="password_screen_timeout")
        raise StepError("Timed out waiting for Google password entry screen")

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="password_entry_screen")

    # Tap password field and type
    try:
        x, y = executor.find_element("Enter your password", retries=2)
        executor.tap(x, y)
    except RuntimeError:
        pass

    time.sleep(0.5)
    executor.type_text(password)
    time.sleep(0.5)
    executor.hide_keyboard()

    # Tap "Next"
    try:
        x, y = executor.find_element("Next", retries=2)
        executor.tap(x, y)
    except RuntimeError:
        executor.press_key(66)  # KEYCODE_ENTER
    time.sleep(3)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="after_password_next")


def _handle_intermediate_screens(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Handle intermediate post-login screens:
      - Terms of Service / Privacy Policy → tap "I agree" or "Accept"
      - Backup options → tap "Skip" or "No thanks"
      - Google Pay setup prompt → tap "No thanks" or "Skip"
      - Any other "Next" / "Continue" prompts

    Loops for up to _TRANSITION_TIMEOUT seconds, tapping through screens
    until success signals appear or timeout.
    """
    logger.info("Handling intermediate screens", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    deadline = time.monotonic() + _TRANSITION_TIMEOUT * 2  # allow up to 2 minutes
    dismissed_count = 0

    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        # Check for success — account is already set up
        if any(signal in texts for signal in _SUCCESS_SIGNALS):
            logger.info(
                "Intermediate screens resolved",
                dismissed=dismissed_count,
                device_id=device_id,
            )
            run_logger.save_screenshot(img, label="intermediate_done")
            return

        # Check for 2FA (safety guard)
        if any(s in texts for s in _2FA_SIGNALS):
            run_logger.save_screenshot(img, label="2fa_detected_intermediate")
            raise StepError(
                "2FA screen appeared during intermediate screen handling."
            )

        # Look for tappable skip/agree/next buttons
        tapped = False
        for result in results:
            word = result.text.lower().strip(".,!?")
            if result.confidence >= 0.5 and word in _INTERMEDIATE_SKIP:
                executor.tap(result.cx, result.cy)
                dismissed_count += 1
                logger.info(
                    "Dismissed intermediate screen",
                    button=result.text,
                    dismissed_count=dismissed_count,
                    device_id=device_id,
                )
                time.sleep(1.5)
                tapped = True
                break  # restart scan after tap

        if not tapped:
            time.sleep(_POLL_INTERVAL)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="intermediate_timeout")
    raise StepError("Timed out handling Google account intermediate screens")


def _verify_account_added(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    email: str,
    device_id: str,
) -> None:
    """Confirm the account appears in Settings → Accounts."""
    logger.info("Verifying account added", email=email, device_id=device_id)

    # Give the UI a moment to settle
    time.sleep(2)
    img = executor.screenshot()
    run_logger.save_screenshot(img, label="account_verify_screen")

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()
    results = ocr.read_region(img)
    texts = " ".join(r.text.lower() for r in results)

    # Check for success signals OR the email address itself
    email_local = email.split("@")[0].lower()
    if (
        any(signal in texts for signal in _SUCCESS_SIGNALS)
        or email_local in texts
        or email.lower() in texts
    ):
        logger.info("Account verification passed", device_id=device_id)
        return

    # Try navigating to the accounts list for a second check
    logger.warning(
        "Account not immediately visible — rechecking accounts list",
        device_id=device_id,
    )
    try:
        executor.driver.execute_script("mobile: startActivity", {
            "intent": "android.settings.SYNC_SETTINGS",
        })
        time.sleep(2)
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="account_verify_settings")

        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)
        if email_local in texts or email.lower() in texts or "google" in texts:
            logger.info("Account verification passed via settings", device_id=device_id)
            return
    except Exception as exc:
        logger.warning("Account verification settings check failed", error=str(exc))

    # Both verification attempts failed — raise to let ScenarioRunner retry
    raise StepError(
        f"Google account '{email}' could not be confirmed in device Settings after login. "
        "Check that credentials are correct and the account has no unexpected security prompts."
    )
