"""
Screen state detector for MLBB automation.

ScreenState enum covers all relevant screens in the Google login → MLBB shop → payment flow.
Detection uses a weighted combination of OCR markers and template matching.
Each screen has a set of required "signals" — the screen is identified when the
minimum number of its signals match with sufficient confidence.

Usage:
    detector = ScreenDetector(ocr=OcrEngine(), matcher=TemplateMatcher())
    state = detector.detect(pil_screenshot)
    print(state)  # e.g. ScreenState.MLBB_SHOP
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from .ocr import OcrEngine
from .template_matcher import TemplateMatcher
from ..logging.logger import get_logger

logger = get_logger(__name__)


class ScreenState(Enum):
    """All reachable screen states during the automation run."""

    GOOGLE_LOGIN = auto()
    GOOGLE_2FA = auto()
    GOOGLE_ACCOUNT_ADDED = auto()
    MLBB_LOADING = auto()
    MLBB_MAIN_MENU = auto()
    MLBB_SHOP = auto()
    MLBB_SHOP_DIAMONDS = auto()
    MLBB_PAYMENT = auto()
    GOOGLE_PAY_SHEET = auto()
    PAYMENT_SUCCESS = auto()
    PAYMENT_FAILED = auto()
    UNKNOWN = auto()


@dataclass
class Signal:
    """
    A single detection signal: either an OCR text presence check or a template match.

    weight:         Contribution to state score (default 1.0).
    required:       If True, this signal must fire for the state to match.
    """

    kind: str  # "ocr" | "template"
    value: str  # text to find OR template name
    min_confidence: float = 0.55
    weight: float = 1.0
    required: bool = False
    bbox: Optional[Tuple[int, int, int, int]] = None  # restrict OCR search region


@dataclass
class StateSpec:
    """Detection specification for one ScreenState."""

    state: ScreenState
    signals: List[Signal]
    min_score: float = 1.0  # minimum weighted score to claim this state


def _ocr_signal(
    text: str,
    confidence: float = 0.55,
    weight: float = 1.0,
    required: bool = False,
    bbox: Optional[Tuple[int, int, int, int]] = None,
) -> Signal:
    return Signal("ocr", text, min_confidence=confidence, weight=weight, required=required, bbox=bbox)


def _tmpl_signal(
    name: str,
    confidence: float = 0.80,
    weight: float = 1.0,
    required: bool = False,
) -> Signal:
    return Signal("template", name, min_confidence=confidence, weight=weight, required=required)


# ---------------------------------------------------------------------------
# State specifications — ordered from most-specific to least-specific.
# ---------------------------------------------------------------------------
_STATE_SPECS: List[StateSpec] = [
    # --- Payment outcomes ---
    StateSpec(ScreenState.PAYMENT_SUCCESS, min_score=1.0, signals=[
        _ocr_signal("payment successful", confidence=0.5, required=True),
    ]),
    StateSpec(ScreenState.PAYMENT_SUCCESS, min_score=1.0, signals=[
        _ocr_signal("purchase complete", confidence=0.5, required=True),
    ]),
    StateSpec(ScreenState.PAYMENT_FAILED, min_score=1.0, signals=[
        _ocr_signal("payment failed", confidence=0.5, required=True),
    ]),
    StateSpec(ScreenState.PAYMENT_FAILED, min_score=1.0, signals=[
        _ocr_signal("transaction declined", confidence=0.5, required=True),
    ]),

    # --- Google Pay bottom sheet ---
    StateSpec(ScreenState.GOOGLE_PAY_SHEET, min_score=1.5, signals=[
        _tmpl_signal("google_pay_logo", confidence=0.75, weight=1.0, required=True),
        _ocr_signal("pay", confidence=0.5, weight=0.5),
    ]),
    StateSpec(ScreenState.GOOGLE_PAY_SHEET, min_score=1.0, signals=[
        _ocr_signal("google pay", confidence=0.5, required=True),
        _ocr_signal("buy with", confidence=0.5, weight=0.5),
    ]),

    # --- MLBB payment selection screen ---
    StateSpec(ScreenState.MLBB_PAYMENT, min_score=1.5, signals=[
        _tmpl_signal("google_pay_logo", confidence=0.75, weight=1.0),
        _ocr_signal("google pay", confidence=0.5, weight=0.5),
        _ocr_signal("select payment", confidence=0.5, weight=0.5),
    ]),

    # --- MLBB diamonds shop ---
    StateSpec(ScreenState.MLBB_SHOP_DIAMONDS, min_score=1.5, signals=[
        _ocr_signal("diamonds", confidence=0.5, required=True, weight=1.0),
        _ocr_signal("buy", confidence=0.5, weight=0.5),
    ]),
    StateSpec(ScreenState.MLBB_SHOP_DIAMONDS, min_score=1.5, signals=[
        _tmpl_signal("buy_button", confidence=0.75, weight=1.0, required=True),
        _ocr_signal("diamonds", confidence=0.5, weight=0.5),
    ]),

    # --- MLBB general shop ---
    StateSpec(ScreenState.MLBB_SHOP, min_score=1.0, signals=[
        _tmpl_signal("shop_icon", confidence=0.75, required=True, weight=1.0),
    ]),
    StateSpec(ScreenState.MLBB_SHOP, min_score=1.0, signals=[
        _ocr_signal("shop", confidence=0.5, required=True, weight=1.0),
        _ocr_signal("diamonds", confidence=0.5, weight=0.5),
    ]),

    # --- MLBB main menu ---
    StateSpec(ScreenState.MLBB_MAIN_MENU, min_score=1.0, signals=[
        _tmpl_signal("main_menu_bg", confidence=0.70, weight=1.0),
        _ocr_signal("classic", confidence=0.5, weight=0.5),
        _ocr_signal("profile", confidence=0.5, weight=0.5),
    ]),
    StateSpec(ScreenState.MLBB_MAIN_MENU, min_score=1.5, signals=[
        _ocr_signal("classic", confidence=0.5, weight=1.0, required=True),
        _ocr_signal("ranked", confidence=0.5, weight=0.5),
    ]),

    # --- MLBB loading / splash ---
    StateSpec(ScreenState.MLBB_LOADING, min_score=1.0, signals=[
        _tmpl_signal("mlbb_loading_logo", confidence=0.70, weight=1.0),
    ]),
    StateSpec(ScreenState.MLBB_LOADING, min_score=1.0, signals=[
        _ocr_signal("loading", confidence=0.5, required=True, weight=1.0),
        _ocr_signal("mobile legends", confidence=0.5, weight=0.5),
    ]),

    # --- Google 2FA (should not occur per requirements but kept as safety) ---
    StateSpec(ScreenState.GOOGLE_2FA, min_score=1.0, signals=[
        _ocr_signal("2-step verification", confidence=0.5, required=True),
    ]),
    StateSpec(ScreenState.GOOGLE_2FA, min_score=1.0, signals=[
        _ocr_signal("verify it's you", confidence=0.5, required=True),
    ]),

    # --- Google account added confirmation ---
    StateSpec(ScreenState.GOOGLE_ACCOUNT_ADDED, min_score=1.0, signals=[
        _ocr_signal("account added", confidence=0.5, required=True),
    ]),

    # --- Google login ---
    StateSpec(ScreenState.GOOGLE_LOGIN, min_score=1.0, signals=[
        _tmpl_signal("google_sign_in_button", confidence=0.75, weight=1.0, required=True),
    ]),
    StateSpec(ScreenState.GOOGLE_LOGIN, min_score=1.5, signals=[
        _ocr_signal("sign in with google", confidence=0.5, weight=1.0, required=True),
        _ocr_signal("google", confidence=0.5, weight=0.5),
    ]),
    StateSpec(ScreenState.GOOGLE_LOGIN, min_score=1.0, signals=[
        _ocr_signal("enter your email", confidence=0.5, required=True, weight=1.0),
    ]),
    StateSpec(ScreenState.GOOGLE_LOGIN, min_score=1.0, signals=[
        _ocr_signal("enter your password", confidence=0.5, required=True, weight=1.0),
    ]),
]


class ScreenDetector:
    """
    Classifies the current Android screen into a ScreenState.

    Strategy:
        1. Run each StateSpec against the screenshot.
        2. A signal fires if OCR finds the text (≥min_confidence) or
           the template matches (≥min_confidence).
        3. Required signals must all fire; otherwise the spec is skipped.
        4. Score = sum of weights of fired signals.
        5. Return the first spec whose score ≥ spec.min_score.
        6. Return UNKNOWN if nothing matches.
    """

    def __init__(
        self,
        ocr: Optional[OcrEngine] = None,
        matcher: Optional[TemplateMatcher] = None,
    ) -> None:
        self._ocr = ocr or OcrEngine()
        self._matcher = matcher or TemplateMatcher()

    def detect(self, screenshot: Image.Image) -> ScreenState:
        """
        Detect the current screen state.

        Args:
            screenshot: Full-screen PIL Image.

        Returns:
            Detected ScreenState (UNKNOWN if no spec matched).
        """
        t0 = time.monotonic()

        # Pre-compute OCR results once (expensive) and cache for this call
        ocr_cache: Dict[Optional[tuple], list] = {}

        def get_ocr(bbox):
            if bbox not in ocr_cache:
                ocr_cache[bbox] = self._ocr.read_region(screenshot, bbox=bbox)
            return ocr_cache[bbox]

        for spec in _STATE_SPECS:
            score = 0.0
            required_ok = True

            for sig in spec.signals:
                fired = False

                if sig.kind == "ocr":
                    ocr_results = get_ocr(sig.bbox)
                    needle = sig.value.lower()
                    for r in ocr_results:
                        if r.confidence >= sig.min_confidence and needle in r.text.lower():
                            fired = True
                            break

                elif sig.kind == "template":
                    match = self._matcher.find(screenshot, sig.value, threshold=sig.min_confidence)
                    fired = match is not None

                if sig.required and not fired:
                    required_ok = False
                    break
                if fired:
                    score += sig.weight

            if required_ok and score >= spec.min_score:
                elapsed = round(time.monotonic() - t0, 3)
                logger.info(
                    "screen_detected",
                    state=spec.state.name,
                    score=round(score, 2),
                    elapsed_s=elapsed,
                )
                return spec.state

        elapsed = round(time.monotonic() - t0, 3)
        logger.info("screen_unknown", elapsed_s=elapsed)
        return ScreenState.UNKNOWN
