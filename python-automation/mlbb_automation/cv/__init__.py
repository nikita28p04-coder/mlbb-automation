"""
Computer Vision and OCR package for MLBB screen navigation.

Modules:
    ocr              - EasyOCR wrapper with lazy init and image preprocessing
    template_matcher - OpenCV multi-scale template matching
    screen_detector  - Screen state enum + heuristic detection
    state_machine    - Transition graph and navigate_to() helper
"""

from .ocr import OcrEngine, OcrResult
from .template_matcher import MatchResult, TemplateMatcher
from .screen_detector import ScreenDetector, ScreenState
from .state_machine import StateMachine

__all__ = [
    "OcrEngine",
    "OcrResult",
    "MatchResult",
    "TemplateMatcher",
    "ScreenDetector",
    "ScreenState",
    "StateMachine",
]
