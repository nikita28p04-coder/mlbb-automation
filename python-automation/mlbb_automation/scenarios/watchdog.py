"""
Background watchdog thread for closing unwanted popups and dialogs.

Monitors the device screen every 2 seconds and automatically dismisses:
  - Android system dialogs ("Allow", "OK", "Deny", "App not responding")
  - MLBB update prompts ("Update Later", "Cancel")
  - MLBB ad banners (close/X button by template or text)
  - Google permission dialogs

The watchdog runs in a daemon thread so it is automatically stopped
when the main process exits. Use as a context manager or call stop() explicitly.

Usage:
    with Watchdog(executor, run_logger=run_logger) as wd:
        # ... automation steps ...

    # Or manually:
    wd = Watchdog(executor)
    wd.start()
    try:
        ...
    finally:
        wd.stop()
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import List, Optional, Tuple

from PIL import Image

from ..cv.ocr import OcrEngine
from ..cv.template_matcher import TemplateMatcher
from ..logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

POLL_INTERVAL_S = 2.0

# ---------------------------------------------------------------------------
# Text patterns that should trigger a dismissal tap
# ---------------------------------------------------------------------------
# Each entry: (text_needle, approximate_tap_coords, is_case_sensitive)
# Coordinates are reference 1080×1920 values; OCR-based tap is preferred.
_DISMISS_TEXTS: List[Tuple[str, Tuple[int, int]]] = [
    # Android system dialogs
    ("app not responding", (180, 1400)),       # "Wait" option
    ("wait", (180, 1400)),
    # Google permission
    ("allow", (720, 1400)),
    ("deny", (360, 1400)),
    # MLBB update prompts
    ("update later", (540, 1300)),
    ("cancel", (360, 1300)),
    ("skip", (540, 1300)),
    ("close", (540, 1300)),
    # Ad banners
    ("×", (1000, 200)),
    ("✕", (1000, 200)),
]

# Text that should never be tapped (false-positive guard)
_NEVER_TAP_TEXT = frozenset({"buy", "pay", "purchase", "confirm", "diamonds"})


class Watchdog:
    """
    Background daemon that polls the device screen and dismisses popups.

    Thread-safety: The stop event is the only shared state between threads.
    All driver interactions happen in the watchdog thread only.
    """

    def __init__(
        self,
        executor,
        run_logger: Optional[RunLogger] = None,
        poll_interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._executor = executor
        self._run_logger = run_logger
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dismissed_count = 0
        self._last_screen_hash: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="mlbb-watchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info("watchdog_started", poll_interval_s=self._poll_interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the watchdog to stop and wait for thread exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("watchdog_stopped", dismissed_total=self._dismissed_count)

    def __enter__(self) -> "Watchdog":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_once()
            except Exception as exc:
                # Never crash the watchdog — log and continue
                logger.warning("watchdog_error", error=str(exc))
            self._stop_event.wait(timeout=self._poll_interval)

    def _check_once(self) -> None:
        """Take a screenshot, hash it, and attempt dismissal if it changed."""
        img = self._executor.screenshot()
        screen_hash = self._image_hash(img)

        if screen_hash == self._last_screen_hash:
            return  # Screen unchanged; skip OCR to save CPU
        self._last_screen_hash = screen_hash

        # Try OCR-based text dismiss first
        if self._try_dismiss_by_text(img):
            return

        # Try template-based dismiss (X buttons etc.)
        self._try_dismiss_by_template(img)

    def _try_dismiss_by_text(self, img: Image.Image) -> bool:
        """Look for known dismiss-text and tap it. Returns True if tapped."""
        ocr = OcrEngine()

        # Only scan top and bottom 30% of screen (where dialogs usually appear)
        w, h = img.size
        top_region = (0, 0, w, int(h * 0.30))
        bottom_region = (0, int(h * 0.70), w, h)

        for region in (top_region, bottom_region):
            results = ocr.read_region(img, bbox=region)
            for r in results:
                if r.confidence < 0.5:
                    continue
                low = r.text.lower().strip()

                # Guard: never tap payment-related text
                if any(guard in low for guard in _NEVER_TAP_TEXT):
                    continue

                for needle, fallback_xy in _DISMISS_TEXTS:
                    if needle in low:
                        logger.info(
                            "watchdog_dismiss",
                            matched_text=r.text,
                            needle=needle,
                            cx=r.cx,
                            cy=r.cy,
                        )
                        if self._run_logger:
                            self._run_logger.log_step(
                                "watchdog_dismiss",
                                status="ok",
                                text=r.text,
                                cx=r.cx,
                                cy=r.cy,
                            )
                        try:
                            self._executor.tap(r.cx, r.cy)
                        except Exception as exc:
                            logger.warning("watchdog_tap_failed", error=str(exc))
                        self._dismissed_count += 1
                        time.sleep(0.5)
                        return True
        return False

    def _try_dismiss_by_template(self, img: Image.Image) -> bool:
        """Try template-based dismiss for close/X button icons."""
        matcher = TemplateMatcher()

        for template_name in ("close_button", "x_button", "dialog_ok"):
            result = matcher.find(img, template_name, threshold=0.75)
            if result is not None:
                logger.info(
                    "watchdog_dismiss_template",
                    template=template_name,
                    cx=result.cx,
                    cy=result.cy,
                    confidence=round(result.confidence, 3),
                )
                try:
                    self._executor.tap(result.cx, result.cy)
                except Exception as exc:
                    logger.warning("watchdog_template_tap_failed", error=str(exc))
                self._dismissed_count += 1
                time.sleep(0.5)
                return True
        return False

    @staticmethod
    def _image_hash(img: Image.Image) -> str:
        """Compute a fast perceptual hash of the image for change detection."""
        thumb = img.resize((16, 16)).convert("L")
        import hashlib
        return hashlib.md5(thumb.tobytes()).hexdigest()
