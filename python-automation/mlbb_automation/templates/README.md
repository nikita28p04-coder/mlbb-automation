# UI Template Images

This directory contains PNG reference images used for OpenCV template matching.

## Current Status

All files are **placeholders** (64×32 light-grey PNG) generated automatically.
Replace them with real screenshots from your target device(s) before running the automation.

## How to Capture a Real Template

1. Take a full screenshot: `exe.screenshot().save("screen.png")`
2. Open in an image editor and crop the exact UI element
3. Save as `<template_name>.png` in this directory
4. Recommended: capture at 1080×1920 reference resolution

## Template Index

| File | Used in | Detects |
|------|---------|---------|
| `google_sign_in_button.png` | screen_detector, state_machine | "Sign in with Google" button |
| `google_pay_logo.png` | screen_detector, state_machine | Google Pay logo on payment sheet |
| `shop_icon.png` | screen_detector, state_machine | MLBB shop icon in main menu nav bar |
| `main_menu_bg.png` | screen_detector | MLBB main menu background indicator |
| `mlbb_loading_logo.png` | screen_detector | MLBB loading screen logo |
| `buy_button.png` | screen_detector, state_machine | "Buy" button in diamonds shop |
| `diamonds_tab.png` | state_machine | "Diamonds" tab in the MLBB shop |
| `close_button.png` | watchdog | Generic close/X button in popups |
| `x_button.png` | watchdog | Ad banner close button |
| `dialog_ok.png` | watchdog | Android system dialog OK button |

## Notes

- Template matching is multi-scale (0.7x–1.3x) so minor resolution differences are tolerated.
- Confidence threshold is 0.80 by default; lower to 0.70 for harder-to-match elements.
- Avoid templates with highly dynamic backgrounds (animations, timers).
