#!/usr/bin/env python3
"""
ADB template capture helper.

Takes a screenshot from a connected Android device via ADB, displays
image metadata, then saves a cropped region to the templates directory.

Usage:
    # Interactive crop (enter coordinates when prompted)
    python scripts/capture_template.py shop_icon

    # Non-interactive crop via CLI args (x y width height)
    python scripts/capture_template.py shop_icon --crop 820 1600 120 120

    # Save raw screenshot only (no crop)
    python scripts/capture_template.py shop_icon --no-crop

    # Specify ADB device (if multiple connected)
    python scripts/capture_template.py shop_icon --device emulator-5554
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow is required. Install with:  pip install Pillow")
    sys.exit(1)


TEMPLATES_DIR = Path(__file__).parent.parent / "mlbb_automation" / "templates"


def _adb_screenshot(device_serial: str | None) -> Image.Image:
    """Capture a screenshot from the connected Android device via ADB."""
    cmd = ["adb"]
    if device_serial:
        cmd += ["-s", device_serial]
    cmd += ["exec-out", "screencap", "-p"]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True)

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        print(f"ERROR: adb screencap failed:\n{stderr}")
        sys.exit(1)

    if not result.stdout:
        print("ERROR: adb returned empty output — is a device connected?")
        print("Run 'adb devices' to check connected devices.")
        sys.exit(1)

    img = Image.open(io.BytesIO(result.stdout))
    return img


def _ask_crop(img: Image.Image) -> tuple[int, int, int, int]:
    """Prompt the user for crop coordinates interactively."""
    w, h = img.size
    print(f"\nDevice screenshot size: {w} x {h} pixels")
    print("Enter the crop region for the template element.")
    print("Tip: Use a screenshot viewer to find x, y coordinates.")
    print()

    def _prompt_int(label: str, default: int | None = None) -> int:
        while True:
            suffix = f" [{default}]" if default is not None else ""
            raw = input(f"  {label}{suffix}: ").strip()
            if not raw and default is not None:
                return default
            try:
                return int(raw)
            except ValueError:
                print("  Please enter an integer.")

    x = _prompt_int("Left edge x")
    y = _prompt_int("Top edge y")
    cw = _prompt_int("Crop width")
    ch = _prompt_int("Crop height")
    return (x, y, cw, ch)


def _save_template(img: Image.Image, name: str, crop: tuple[int, int, int, int] | None) -> Path:
    """Crop (if requested) and save the template PNG."""
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TEMPLATES_DIR / f"{name}.png"

    if crop is not None:
        x, y, cw, ch = crop
        w, h = img.size
        x2, y2 = min(x + cw, w), min(y + ch, h)
        region = img.crop((x, y, x2, y2))
        print(f"\nCropped region: ({x}, {y}) → ({x2}, {y2})  size={region.size}")
        region.save(out_path)
    else:
        img.save(out_path)
        print(f"\nSaved full screenshot ({img.size[0]}x{img.size[1]}): {out_path}")

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture an Android device screenshot and save a cropped template.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("name", help="Template name (without .png extension)")
    parser.add_argument(
        "--crop",
        nargs=4,
        metavar=("X", "Y", "W", "H"),
        type=int,
        default=None,
        help="Crop rectangle: left top width height (pixels)",
    )
    parser.add_argument(
        "--no-crop",
        action="store_true",
        help="Save full screenshot without cropping",
    )
    parser.add_argument(
        "--device",
        default=None,
        metavar="SERIAL",
        help="ADB device serial (from 'adb devices'). Required if multiple devices connected.",
    )
    parser.add_argument(
        "--save-raw",
        metavar="PATH",
        default=None,
        help="Also save the raw (uncropped) screenshot to this path.",
    )
    args = parser.parse_args()

    print(f"\nCapturing screenshot from {'device ' + args.device if args.device else 'default ADB device'}...")
    img = _adb_screenshot(args.device)
    print(f"Screenshot captured: {img.size[0]} x {img.size[1]} px")

    if args.save_raw:
        img.save(args.save_raw)
        print(f"Raw screenshot saved: {args.save_raw}")

    if args.no_crop:
        out = _save_template(img, args.name, crop=None)
    elif args.crop:
        out = _save_template(img, args.name, crop=tuple(args.crop))
    else:
        crop = _ask_crop(img)
        out = _save_template(img, args.name, crop=crop)

    print(f"\nTemplate saved: {out}")
    print(f"  Size: {Image.open(out).size[0]} x {Image.open(out).size[1]} px")
    print("\nDone. Verify the template looks correct before running automation.")


if __name__ == "__main__":
    main()
