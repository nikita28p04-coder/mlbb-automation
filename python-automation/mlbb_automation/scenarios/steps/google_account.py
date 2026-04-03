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

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Optional

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

SETTINGS_PACKAGE = "com.android.settings"
SETTINGS_ACCOUNTS_ACTIVITY = "com.android.settings.accounts.AccountSettings"

# Text fragments searched via OCR / UI hierarchy — tolerant of minor UI variations
# Both English and Russian variants are included (devices may have either locale).
_EMAIL_PROMPTS = (
    "enter your email", "sign in", "add your email",
    "введите email", "введите адрес", "войдите в аккаунт", "добавьте email",
    "адрес электронной", "эл. адрес",
)
_PASSWORD_PROMPTS = ("enter your password", "password", "введите пароль", "пароль")
_INTERMEDIATE_SKIP = (
    # English
    "skip", "no thanks", "not now", "later", "decline",
    "i agree", "accept", "agree", "continue", "next", "done", "got it", "ok",
    # Russian
    "пропустить", "нет", "не сейчас", "позже", "отклонить",
    "принять", "согласен", "принимаю", "продолжить", "далее",
    "готово", "понятно", "ок", "хорошо", "спасибо",
)
_SUCCESS_SIGNALS = (
    "account added", "sync", "google account", "gmail",
    "аккаунт добавлен", "синхронизация", "аккаунт google", "gmail",
)
_2FA_SIGNALS = (
    "verify", "2-step", "two-step", "verification code", "authenticator",
    "подтверждение", "двухэтапная", "код подтверждения",
)

# Max seconds to wait for each major state transition
_TRANSITION_TIMEOUT = 60
_POLL_INTERVAL = 2.0


class StepError(Exception):
    """Non-recoverable error in a step."""


def _tap_next(executor: "AppiumExecutor") -> None:
    """Tap the 'Next' / 'Далее' button, falling back to ENTER key."""
    for text in ("Next", "Далее", "next", "далее"):
        try:
            x, y = executor.find_element(text, retries=1)
            executor.tap(x, y)
            return
        except RuntimeError:
            continue
    executor.press_key(66)  # KEYCODE_ENTER as last resort


def _is_google_signin_active(executor: AppiumExecutor) -> bool:
    """
    Return True if a Google sign-in screen is already the foreground activity.
    Checks the current package name via Appium.
    """
    try:
        pkg = executor.driver.current_package
        return pkg in (
            "com.google.android.gms",
            "com.google.android.accounts",
        )
    except Exception:
        return False


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    email: str,
    password: str,
    device_id: str = "",
) -> None:
    """
    Add a Google account to the device via Android Settings.

    Handles two entry points:
    - Fresh start: navigates Settings → Accounts → Add Account → Google
    - Resume: Google sign-in WebView already open (left over from a previous run)

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        email:      Google account email.
        password:   Google account password (no 2FA).
        device_id:  Device ID for log context.
    """
    run_logger.log_step("google_account", "started", device_id=device_id)
    logger.info("google_account starting", device_id=device_id, email=email)

    if _is_google_signin_active(executor):
        # Google sign-in WebView is already open from a prior run — resume directly
        logger.info(
            "Google sign-in already active — skipping Settings navigation",
            device_id=device_id,
        )
        run_logger.log_step("google_account", "resume_from_signin", device_id=device_id)
    else:
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
    # Wait longer for Samsung Settings to fully render its accessibility tree
    time.sleep(5)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="settings_accounts")


_ADD_ACCOUNT_TEXTS = (
    "Добавить учетную запись",  # Samsung Russian (most specific first)
    "Add account",               # English stock Android
    "Добавить аккаунт",         # Alternative Russian
    "Add Account",
    "Добавить",                  # Fallback prefix
)


def _find_and_tap_uiautomator(executor: AppiumExecutor, text: str) -> bool:
    """Try to find and tap element using UiAutomator2 selector (most reliable for Samsung)."""
    from appium.webdriver.common.appiumby import AppiumBy
    try:
        el = executor.driver.find_element(
            AppiumBy.ANDROID_UIAUTOMATOR,
            f'new UiSelector().textContains("{text}")'
        )
        # Get clickable parent if needed
        if el.get_attribute("clickable") == "false":
            # Try clicking anyway (parent container is usually clickable at same coords)
            loc = el.location
            sz = el.size
            cx = loc["x"] + sz["width"] // 2
            cy = loc["y"] + sz["height"] // 2
            executor.tap(cx, cy)
        else:
            el.click()
        return True
    except Exception:
        return False


def _mobile_shell_tap(executor: AppiumExecutor, x: int, y: int) -> bool:
    """Tap via Appium mobile:shell ADB bridge — bypasses UiAutomator2 element search.

    This goes through Appium's own ADB client (not the UiAutomator2 instrumentation),
    so it works even when UiAutomator2 element search is broken.
    """
    try:
        executor.driver.execute_script("mobile: shell", {
            "command": "input",
            "args": ["tap", str(x), str(y)],
        })
        logger.debug("mobile:shell tap at (%d, %d) succeeded", x, y)
        return True
    except Exception as exc:
        logger.warning("mobile:shell tap at (%d, %d) failed: %s", x, y, exc)
        return False


def _adb_subprocess_tap(serial: str, x: int, y: int) -> bool:
    """Tap via direct subprocess ADB call — fully bypasses Appium and UiAutomator2."""
    try:
        result = subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(x), str(y)],
            capture_output=True,
            timeout=15,
        )
        ok = result.returncode == 0
        logger.debug("subprocess ADB tap (%d,%d) rc=%d", x, y, result.returncode)
        return ok
    except Exception as exc:
        logger.warning("subprocess ADB tap (%d,%d) failed: %s", x, y, exc)
        return False


def _adb_dump_find_coords(serial: str, search_texts: tuple) -> Optional[tuple]:
    """Use uiautomator dump via subprocess ADB to find element coordinates by text.

    Returns (cx, cy) of first matching element, or None if not found.
    Note: may fail if Appium's UiAutomator2 server is holding the UIAutomator lock.
    """
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "uiautomator", "dump", "--compressed", "/sdcard/uidump.xml"],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or b"Killed" in r.stdout or b"ERROR" in r.stdout:
            logger.warning("uiautomator dump failed: %s", r.stdout[:200])
            return None
        # Pull XML to local temp file
        subprocess.run(
            ["adb", "-s", serial, "pull", "/sdcard/uidump.xml", "/tmp/uidump_findcoords.xml"],
            capture_output=True, timeout=15,
        )
        tree = ET.parse("/tmp/uidump_findcoords.xml")
        for node in tree.getroot().iter("node"):
            node_text = node.get("text", "")
            for search in search_texts:
                if search.lower() in node_text.lower():
                    bounds = node.get("bounds", "")
                    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                    if m:
                        cx = (int(m.group(1)) + int(m.group(3))) // 2
                        cy = (int(m.group(2)) + int(m.group(4))) // 2
                        logger.info(
                            "ADB dump found %r at bounds %s → (%d,%d)",
                            node_text, bounds, cx, cy,
                        )
                        return cx, cy
    except Exception as exc:
        logger.warning("ADB dump find failed: %s", exc)
    return None


def _tap_add_account(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Tap 'Add Account' then select 'Google' — supports EN and RU device locale.

    Uses a layered fallback strategy:
      Stage A: UiAutomator2 textContains selector (fast, works when UiA2 is healthy)
      Stage B: mobile:shell ADB tap at known coords (bypasses UiA2 element search)
      Stage C: subprocess ADB tap at known coords (fully bypasses Appium)
    """
    logger.info("Tapping Add Account", device_id=device_id)

    # Known coordinates for "Добавить учетную запись" on Samsung Galaxy A13 1080×2408
    # Verified via ADB uiautomator dump: bounds=[226,342][808,408]
    ADD_ACCT_X, ADD_ACCT_Y = 517, 375

    tapped = False

    # ── Stage A: UiAutomator2 textContains ──────────────────────────────────
    for text in _ADD_ACCOUNT_TEXTS:
        if _find_and_tap_uiautomator(executor, text):
            logger.info(
                "Stage A: Tapped 'Add account' via UiAutomator2 text=%r", text,
                device_id=device_id,
            )
            tapped = True
            break

    # ── Stage B: mobile:shell ADB tap at known coords ───────────────────────
    if not tapped:
        logger.info(
            "Stage A failed — trying Stage B: mobile:shell tap at (%d,%d)",
            ADD_ACCT_X, ADD_ACCT_Y,
            device_id=device_id,
        )
        if _mobile_shell_tap(executor, ADD_ACCT_X, ADD_ACCT_Y):
            tapped = True
            logger.info("Stage B: tapped via mobile:shell", device_id=device_id)

    # ── Stage C: subprocess ADB tap ─────────────────────────────────────────
    if not tapped:
        serial = getattr(executor, "_adb_serial", None) or "adb.mobfarm.selectel.ru:9049"
        logger.info(
            "Stage B failed — trying Stage C: subprocess ADB tap at (%d,%d) serial=%s",
            ADD_ACCT_X, ADD_ACCT_Y, serial,
            device_id=device_id,
        )
        if _adb_subprocess_tap(serial, ADD_ACCT_X, ADD_ACCT_Y):
            tapped = True
            logger.info("Stage C: tapped via subprocess ADB", device_id=device_id)

    if not tapped:
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="add_account_not_found")
        raise StepError(
            f"Could not tap 'Add account' button via UiAutomator2, mobile:shell, or ADB. "
            f"Tried coords ({ADD_ACCT_X},{ADD_ACCT_Y}) and texts: "
            + ", ".join(repr(t) for t in _ADD_ACCOUNT_TEXTS)
        )

    time.sleep(2)
    img = executor.screenshot()
    run_logger.save_screenshot(img, label="add_account_list")

    # ── Select "Google" from the account type chooser ───────────────────────
    _select_google_account_type(executor, run_logger, device_id)


def _select_google_account_type(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Select 'Google' from the account type chooser after tapping 'Add account'."""
    logger.info("Selecting Google account type", device_id=device_id)

    # Stage A: OCR / Appium hierarchy search (works when on the chooser screen)
    try:
        x, y = executor.find_element("Google", retries=3)
        executor.tap(x, y)
        logger.info("Selected Google via find_element", device_id=device_id)
        time.sleep(2)
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="google_account_type")
        return
    except (RuntimeError, Exception) as exc:
        logger.warning("find_element('Google') failed: %s — trying ADB fallback", exc,
                       device_id=device_id)

    # Stage B: ADB dump to find Google's coordinates
    serial = getattr(executor, "_adb_serial", None) or "adb.mobfarm.selectel.ru:9049"
    coords = _adb_dump_find_coords(serial, ("Google",))
    if coords:
        gx, gy = coords
        logger.info("Found Google via ADB dump at (%d,%d)", gx, gy, device_id=device_id)
        _mobile_shell_tap(executor, gx, gy) or _adb_subprocess_tap(serial, gx, gy)
        time.sleep(2)
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="google_account_type")
        return

    # Stage C: Hardcoded fallback (Google is usually near top of list ~y=350)
    logger.warning("ADB dump didn't find Google — tapping estimated coords (540,350)",
                   device_id=device_id)
    _mobile_shell_tap(executor, 540, 350) or _adb_subprocess_tap(serial, 540, 350)
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

    # Focus and fill the email field via UiAutomator2 (most reliable)
    _fill_edittext_uiautomator(executor, email)

    # Tap "Next" / "Далее" / "Next" button
    _tap_button_uiautomator(executor, ("Далее", "Next", "Следующий"))

    time.sleep(2)
    img = executor.screenshot()
    run_logger.save_screenshot(img, label="after_email_next")


def _fill_edittext_uiautomator(
    executor: AppiumExecutor, text: str, password_field: bool = False
) -> None:
    """Focus first visible EditText (or passwordField) and type text into it via UiAutomator2."""
    from appium.webdriver.common.appiumby import AppiumBy
    # Try specific selector first, then fallback to generic EditText
    selectors = []
    if password_field:
        selectors.append('new UiSelector().passwordField(true)')
    selectors.append('new UiSelector().className("android.widget.EditText").instance(0)')

    for selector in selectors:
        try:
            el = executor.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, selector)
            el.click()
            time.sleep(0.5)
            el.clear()
            el.send_keys(text)
            time.sleep(0.5)
            logger.debug("Filled EditText via UiAutomator2 selector=%r", selector)
            return
        except Exception:
            continue

    logger.warning("_fill_edittext_uiautomator: all selectors failed, using type_text fallback")
    executor.type_text(text)


def _tap_button_uiautomator(executor: AppiumExecutor, button_texts: tuple) -> None:
    """Tap a button by text using UiAutomator2, trying multiple text variants."""
    from appium.webdriver.common.appiumby import AppiumBy
    for btn_text in button_texts:
        try:
            el = executor.driver.find_element(
                AppiumBy.ANDROID_UIAUTOMATOR,
                f'new UiSelector().text("{btn_text}")'
            )
            el.click()
            logger.debug("Tapped button via UiAutomator2: %r", btn_text)
            return
        except Exception:
            continue
    # Fallback: ENTER key
    logger.warning("Button not found via UiAutomator2, pressing ENTER as fallback")
    executor.press_key(66)


def _enter_password(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    password: str,
    device_id: str,
) -> None:
    """Wait for the password screen and type the password."""
    logger.info("Entering password", device_id=device_id)

    # Wait for password screen to appear.
    # Use 2 strategies in order:
    #   1. UiAutomator2 passwordField(true) — detects the hidden-character password EditText
    #   2. OCR detection of "пароль" / "password" text
    from appium.webdriver.common.appiumby import AppiumBy
    from ...cv.ocr import OcrEngine

    # Give the transition from email→password screen some time to complete
    time.sleep(5)

    deadline = time.monotonic() + _TRANSITION_TIMEOUT
    password_field_found = False

    while time.monotonic() < deadline:
        img = executor.screenshot()

        # Safety: Check for 2FA signals via OCR
        ocr = OcrEngine()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)
        if any(s in texts for s in _2FA_SIGNALS):
            run_logger.save_screenshot(img, label="unexpected_2fa_screen")
            raise StepError(
                "2FA screen appeared unexpectedly. "
                "This Google account appears to have 2FA enabled."
            )

        # Strategy 1: password-type EditText (definitive — only appears on password screen)
        try:
            executor.driver.find_element(
                AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiSelector().passwordField(true)'
            )
            password_field_found = True
            break
        except Exception:
            pass

        # Strategy 2: OCR-based detection
        if any(prompt in texts for prompt in _PASSWORD_PROMPTS):
            password_field_found = True
            break

        time.sleep(_POLL_INTERVAL)

    if not password_field_found:
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="password_screen_timeout")
        raise StepError("Timed out waiting for Google password entry screen")

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="password_entry_screen")

    # Fill the password field via UiAutomator2 — target passwordField specifically
    _fill_edittext_uiautomator(executor, password, password_field=True)
    executor.hide_keyboard()

    # Tap "Next" / "Далее"
    _tap_button_uiautomator(executor, ("Далее", "Next", "Следующий"))
    time.sleep(3)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="after_password_next")


_INTERMEDIATE_BUTTON_TEXTS = (
    # Accept/Agree — Terms of Service
    "Принять", "Accept", "I agree", "Agree", "Согласен", "Согласна",
    # Skip options
    "Пропустить", "Skip", "No thanks", "Not now", "Нет", "Позже",
    # Continue/Next
    "Далее", "Next", "Продолжить", "Continue",
    # Done/OK
    "Готово", "Done", "OK", "Ок", "ОК", "Понятно", "Хорошо",
)


def _handle_intermediate_screens(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Handle intermediate post-login screens (Terms, Backup, Welcome, etc.).

    Strategy:
      1. Check if the current package is com.android.settings — we are done.
      2. Otherwise tap any visible skip/accept/next button via UiAutomator2.
      3. Repeat until back in Settings or 3-minute timeout.
    """
    logger.info("Handling intermediate screens", device_id=device_id)

    deadline = time.monotonic() + _TRANSITION_TIMEOUT * 3  # 3-minute budget
    dismissed_count = 0
    last_pkg = ""

    while time.monotonic() < deadline:
        # --- Primary exit condition: returned to Settings ----------------------
        try:
            pkg = executor.driver.current_package or ""
        except Exception:
            pkg = ""

        print(f"[intermediate] pkg={pkg} dismissed={dismissed_count}", flush=True)

        if pkg == "com.android.settings":
            logger.info(
                "Returned to Settings — account addition complete",
                dismissed=dismissed_count,
                device_id=device_id,
            )
            img = executor.screenshot()
            run_logger.save_screenshot(img, label="intermediate_done")
            return

        # --- Try to tap any skip/accept/next button ----------------------------
        tapped = False
        for btn_text in _INTERMEDIATE_BUTTON_TEXTS:
            try:
                from appium.webdriver.common.appiumby import AppiumBy
                el = executor.driver.find_element(
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    f'new UiSelector().text("{btn_text}")'
                )
                el.click()
                dismissed_count += 1
                logger.info(
                    "Dismissed intermediate screen",
                    button=btn_text,
                    dismissed_count=dismissed_count,
                    device_id=device_id,
                )
                time.sleep(2)
                tapped = True
                break
            except Exception:
                continue

        if not tapped:
            if pkg != last_pkg:
                img = executor.screenshot()
                run_logger.save_screenshot(img, label=f"intermediate_pkg_{pkg.replace('.', '_')}")
            time.sleep(_POLL_INTERVAL)

        last_pkg = pkg

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
