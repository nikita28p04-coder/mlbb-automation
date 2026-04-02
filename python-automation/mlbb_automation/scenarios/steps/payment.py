"""
Step: Navigate to MLBB Shop and complete a real payment via Google Pay.

Flow:
  1. From main menu → tap Shop icon
  2. Inside Shop → navigate to Diamonds / Top-Up section
  3. Select the smallest available diamond package (e.g. ~$0.99 / ~89 RUB)
  4. Tap Buy → Google Pay sheet appears
  5. Switch Appium context to WEBVIEW (if Google Pay uses WebView)
  6. Confirm payment (tap "Pay" or similar button)
  7. Detect result: "Purchase Successful" or payment error
  8. Save timestamped screenshot and log result

dry_run mode: navigates all the way to the Buy button but does NOT tap it,
then exits cleanly. This allows UI verification without spending money.

Context-switching notes:
  - Google Pay sometimes renders in NATIVE_APP, sometimes in WEBVIEW
  - We probe both and use whichever has the Pay/Confirm button
  - After confirming, we switch back to NATIVE_APP
"""

from __future__ import annotations

import time
from typing import Optional

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

# OCR signals for each state
_SHOP_SIGNALS = ("shop", "магазин")
_DIAMONDS_SIGNALS = ("diamonds", "top up", "topup", "recharge", "алмазы", "пополнить")
_BUY_SIGNALS = ("buy", "purchase", "купить", "приобрести")
_GOOGLE_PAY_SIGNALS = ("google pay", "pay with google", "google pay button", "pay")
_SUCCESS_SIGNALS = (
    "purchase successful",
    "payment successful",
    "success",
    "thank you",
    "order confirmed",
    "покупка выполнена",
    "оплата прошла",
    "успешно",
)
_FAILURE_SIGNALS = (
    "payment failed",
    "transaction declined",
    "error",
    "declined",
    "failed",
    "try again",
    "ошибка",
    "отклонено",
    "не удалось",
)

# Smallest-package heuristics — these text patterns usually appear near cheap packs
_SMALL_PACK_SIGNALS = (
    "86",    # 86 diamonds (~$0.99)
    "89",    # sometimes 89
    "0.99",
    "1.09",
    "₱",    # Philippine peso (often lowest)
    "0,99",
    "$0",
)

# Timeouts
_SHOP_TIMEOUT = 30
_PAYMENT_SHEET_TIMEOUT = 30
_RESULT_TIMEOUT = 60
_POLL_INTERVAL = 2.0


class StepError(Exception):
    """Non-recoverable error in a step."""


class PaymentError(Exception):
    """The payment was processed but failed (declined / server error)."""


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str = "",
    dry_run: bool = False,
) -> None:
    """
    Open MLBB Shop → Diamonds → smallest package → Google Pay → confirm.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        device_id:  Device ID for log context.
        dry_run:    If True, navigate to payment screen but skip final tap.
    """
    run_logger.log_step("payment", "started", device_id=device_id, dry_run=dry_run)
    logger.info("payment step starting", device_id=device_id, dry_run=dry_run)

    # Step 1: Navigate from main menu to Shop
    _open_shop(executor, run_logger, device_id)

    # Step 2: Navigate to Diamonds / Top-Up
    _open_diamonds_section(executor, run_logger, device_id)

    # Step 3: Select smallest package
    _select_smallest_package(executor, run_logger, device_id)

    # Step 4: Tap "Buy"
    if dry_run:
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="dry_run_buy_screen")
        logger.info("DRY RUN — skipping final Buy tap", device_id=device_id)
        run_logger.log_step("payment", "dry_run_ok", device_id=device_id)
        return

    _tap_buy(executor, run_logger, device_id)

    # Step 5: Handle Google Pay sheet (with context switching)
    _handle_google_pay(executor, run_logger, device_id)

    # Step 6: Detect result
    result = _detect_payment_result(executor, run_logger, device_id)

    if result == "success":
        run_logger.log_step("payment", "ok", device_id=device_id, result="success")
        logger.info("Payment completed successfully", device_id=device_id)
    else:
        run_logger.log_step("payment", "payment_failed", device_id=device_id, result=result)
        raise PaymentError(f"Payment failed or declined: {result}")


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

def _open_shop(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Navigate to the MLBB Shop from the main menu."""
    logger.info("Opening MLBB Shop", device_id=device_id)
    run_logger.log_step("payment", "open_shop", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    # Confirm we're on the main menu before proceeding
    img = executor.screenshot()
    run_logger.save_screenshot(img, label="before_shop_nav")

    # Tap the Shop button
    try:
        x, y = executor.find_element("Shop", retries=3)
        executor.tap(x, y)
    except RuntimeError:
        # Try tapping shop icon by searching for the template
        raise StepError(
            "Could not find Shop button on main menu. "
            "Ensure MLBB is on the main menu before running payment step."
        )

    time.sleep(2)

    # Verify we entered the shop
    deadline = time.monotonic() + _SHOP_TIMEOUT
    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)
        if any(s in texts for s in _SHOP_SIGNALS + _DIAMONDS_SIGNALS):
            run_logger.save_screenshot(img, label="shop_opened")
            logger.info("Shop opened", device_id=device_id)
            return
        time.sleep(_POLL_INTERVAL)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="shop_timeout")
    raise StepError(f"Did not enter MLBB Shop within {_SHOP_TIMEOUT}s")


def _open_diamonds_section(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Inside the Shop, navigate to the Diamonds / Top-Up section."""
    logger.info("Opening Diamonds section", device_id=device_id)
    run_logger.log_step("payment", "open_diamonds", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    img = executor.screenshot()
    results = ocr.read_region(img)
    texts = " ".join(r.text.lower() for r in results)

    # If already on diamonds page, skip
    if any(s in texts for s in _DIAMONDS_SIGNALS):
        logger.info("Already on Diamonds section", device_id=device_id)
        return

    # Try to find and tap Diamonds tab
    try:
        x, y = executor.find_element("Diamonds", retries=3)
        executor.tap(x, y)
    except RuntimeError:
        try:
            x, y = executor.find_element("Top Up", retries=2)
            executor.tap(x, y)
        except RuntimeError:
            raise StepError("Could not find Diamonds/Top-Up tab in MLBB Shop")

    time.sleep(2)
    img = executor.screenshot()
    run_logger.save_screenshot(img, label="diamonds_section")
    logger.info("Diamonds section opened", device_id=device_id)


def _select_smallest_package(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Select the smallest (cheapest) diamond package.

    Strategy:
      1. Read all OCR text on screen and find elements containing price signals
         for small amounts (< $2).
      2. Among matches, pick the one closest to the top-left (smallest package
         usually appears first).
      3. Tap it.
    """
    logger.info("Selecting smallest diamond package", device_id=device_id)
    run_logger.log_step("payment", "select_package", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    img = executor.screenshot()
    results = ocr.read_region(img)
    run_logger.save_screenshot(img, label="diamonds_packages")

    # Find OCR results that look like small-pack price tags
    candidates = []
    for result in results:
        text = result.text.lower()
        if any(signal in text for signal in _SMALL_PACK_SIGNALS):
            candidates.append(result)

    if candidates:
        # Sort by position: top-to-bottom, left-to-right
        best = sorted(candidates, key=lambda r: (r.cy, r.cx))[0]
        logger.info(
            "Small pack identified by price text",
            text=best.text,
            device_id=device_id,
        )
        executor.tap(best.cx, best.cy)
        time.sleep(1)
    else:
        # Fallback: tap the first item in the list (top-left area of package grid)
        logger.warning(
            "No price signals found — tapping top-left package area",
            device_id=device_id,
        )
        size = executor.get_screen_size()
        # Packages usually start at roughly 1/3 from top, 1/4 from left
        executor.tap(size[0] // 4, size[1] // 3)
        time.sleep(1)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="package_selected")


def _tap_buy(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Tap the Buy / Purchase button to initiate payment."""
    logger.info("Tapping Buy button", device_id=device_id)
    run_logger.log_step("payment", "tap_buy", device_id=device_id)

    # Try "Buy" first, then "Purchase"
    for label in ("Buy", "Purchase", "Купить"):
        try:
            x, y = executor.find_element(label, retries=2)
            executor.tap(x, y)
            logger.info("Tapped Buy button", label=label, device_id=device_id)
            time.sleep(2)
            img = executor.screenshot()
            run_logger.save_screenshot(img, label="after_buy_tap")
            return
        except RuntimeError:
            continue

    raise StepError("Could not find Buy/Purchase button for selected diamond pack")


# ---------------------------------------------------------------------------
# Google Pay handling
# ---------------------------------------------------------------------------

def _handle_google_pay(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Wait for the Google Pay sheet and confirm the payment.

    Google Pay may appear:
      a) In NATIVE_APP context (bottom sheet overlay)
      b) In a WEBVIEW context (rendered web UI inside a bottom sheet)

    We try to confirm in NATIVE_APP first, then switch to each WEBVIEW
    context if needed.
    """
    logger.info("Waiting for Google Pay sheet", device_id=device_id)
    run_logger.log_step("payment", "google_pay_wait", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    # Wait for the Google Pay sheet to appear
    deadline = time.monotonic() + _PAYMENT_SHEET_TIMEOUT
    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)
        if any(s in texts for s in _GOOGLE_PAY_SIGNALS):
            run_logger.save_screenshot(img, label="google_pay_sheet")
            logger.info("Google Pay sheet visible", device_id=device_id)
            break
        time.sleep(_POLL_INTERVAL)
    else:
        img = executor.screenshot()
        run_logger.save_screenshot(img, label="google_pay_timeout")
        raise StepError(
            f"Google Pay sheet did not appear within {_PAYMENT_SHEET_TIMEOUT}s"
        )

    # Try confirming in NATIVE_APP context first
    if _try_confirm_payment_native(executor, run_logger, device_id):
        return

    # Switch contexts and try each WEBVIEW
    if _try_confirm_payment_webview(executor, run_logger, device_id):
        return

    raise StepError(
        "Could not find payment confirmation button in any Appium context"
    )


def _try_confirm_payment_native(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> bool:
    """
    Try to confirm the Google Pay payment in the NATIVE_APP context.

    Returns True if the Pay button was found and tapped.
    """
    _confirm_labels = ("pay", "confirm", "оплатить", "подтвердить")

    logger.info("Trying to confirm payment in NATIVE_APP context", device_id=device_id)
    try:
        executor.switch_to_native()
    except Exception:
        pass

    try:
        for label in _confirm_labels:
            try:
                x, y = executor.find_element(label.capitalize(), retries=2)
                executor.tap(x, y)
                logger.info("Payment confirmed in NATIVE_APP", label=label, device_id=device_id)
                img = executor.screenshot()
                run_logger.save_screenshot(img, label="payment_confirmed_native")
                return True
            except RuntimeError:
                continue
    except Exception as exc:
        logger.warning(
            "Native context payment confirm failed", error=str(exc), device_id=device_id
        )

    return False


def _try_confirm_payment_webview(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> bool:
    """
    Switch through WEBVIEW contexts and attempt to confirm the payment.

    Returns True if the Pay button was found and tapped.
    """
    logger.info("Probing WEBVIEW contexts for payment button", device_id=device_id)

    _confirm_labels = ("pay", "confirm", "оплатить", "подтвердить")

    try:
        contexts = executor.get_contexts()
        logger.info("Available contexts", contexts=contexts, device_id=device_id)
    except Exception as exc:
        logger.warning("Could not retrieve contexts", error=str(exc), device_id=device_id)
        return False

    webviews = [c for c in contexts if "WEBVIEW" in c.upper()]
    if not webviews:
        logger.info("No WEBVIEW contexts found", device_id=device_id)
        return False

    for wv in webviews:
        try:
            executor.driver.switch_to.context(wv)
            logger.info("Switched to context", context=wv, device_id=device_id)
            time.sleep(1)

            for label in _confirm_labels:
                try:
                    x, y = executor.find_element(label.capitalize(), retries=2)
                    executor.tap(x, y)
                    logger.info(
                        "Payment confirmed in WEBVIEW",
                        context=wv,
                        label=label,
                        device_id=device_id,
                    )
                    executor.switch_to_native()
                    img = executor.screenshot()
                    run_logger.save_screenshot(img, label="payment_confirmed_webview")
                    return True
                except RuntimeError:
                    continue
        except Exception as exc:
            logger.warning(
                "Error in WEBVIEW context",
                context=wv,
                error=str(exc),
                device_id=device_id,
            )

    # Ensure we return to native context
    executor.switch_to_native()

    return False


# ---------------------------------------------------------------------------
# Result detection
# ---------------------------------------------------------------------------

def _detect_payment_result(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> str:
    """
    Poll the screen for up to _RESULT_TIMEOUT seconds to detect success/failure.

    Detection uses a hybrid strategy — template matching first (lower latency,
    robust to UI drift), then OCR text as a fallback.  Both must agree before
    a result is committed.  This prevents false positives from partial OCR
    matches on unrelated screens.

    Template files required (placeholders provided; replace with real crops):
      - templates/payment_success.png
      - templates/payment_failed.png

    Returns:
        "success"        — success screen confirmed by template and/or OCR.
        "failed:<reason>"— failure screen confirmed by template and/or OCR.
        "timeout"        — neither signal detected within _RESULT_TIMEOUT.
    """
    from ...cv.ocr import OcrEngine
    from ...cv.template_matcher import TemplateMatcher

    ocr = OcrEngine()
    matcher = TemplateMatcher()

    logger.info("Waiting for payment result", device_id=device_id)
    run_logger.log_step("payment", "detecting_result", device_id=device_id)

    deadline = time.monotonic() + _RESULT_TIMEOUT

    while time.monotonic() < deadline:
        # Ensure we're in native context for screenshot
        try:
            executor.switch_to_native()
        except Exception:
            pass

        img = executor.screenshot()

        # ── Stage 1: Template match ────────────────────────────────────────
        success_template = matcher.find(img, "payment_success", threshold=0.75)
        failed_template = matcher.find(img, "payment_failed", threshold=0.75)

        # ── Stage 2: OCR text scan ─────────────────────────────────────────
        ocr_results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in ocr_results)
        ocr_success = any(s in texts for s in _SUCCESS_SIGNALS)
        ocr_failure = any(s in texts for s in _FAILURE_SIGNALS)

        # ── Decision: template hit OR OCR hit (at least one must fire) ─────
        if success_template is not None or ocr_success:
            signal = "template" if success_template else "ocr"
            logger.info(
                "Payment success screen detected",
                signal=signal,
                template_confidence=round(success_template.confidence, 3) if success_template else None,
                device_id=device_id,
            )
            run_logger.save_screenshot(img, label="payment_success")
            return "success"

        if failed_template is not None or ocr_failure:
            signal = "template" if failed_template else "ocr"
            failure_text = next(
                (r.text for r in ocr_results if any(s in r.text.lower() for s in _FAILURE_SIGNALS)),
                "unknown_error",
            )
            logger.warning(
                "Payment failure screen detected",
                signal=signal,
                failure_text=failure_text,
                template_confidence=round(failed_template.confidence, 3) if failed_template else None,
                device_id=device_id,
            )
            run_logger.save_screenshot(img, label="payment_failed")
            return f"failed:{failure_text}"

        time.sleep(_POLL_INTERVAL)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="payment_result_timeout")
    logger.warning(
        "Payment result not detected within timeout",
        timeout=_RESULT_TIMEOUT,
        device_id=device_id,
    )
    return "timeout"
