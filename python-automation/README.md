# mlbb_automation

Android device farm automation for **Mobile Legends: Bang Bang**.

Connects to the Selectel Mobile Farm, reserves an Android device, adds a Google account, installs MLBB, navigates the game using computer vision + OCR, and performs a real payment via Google Pay.

## Quick start

```bash
cd python-automation
pip install -e .

# Copy and fill in your credentials
cp config.example.yaml config.yaml
# Edit config.yaml: set selectel_api_key, google_email, google_password

# Validate credentials and connectivity before a real run
mlbb-automation check --config config.yaml

# List available devices on the farm
mlbb-automation devices --config config.yaml

# Dry run — navigate to payment but skip the final tap
mlbb-automation run --config config.yaml --dry-run

# Run the full scenario (real ~$0.99 payment)
mlbb-automation run --config config.yaml

# Run a single step for debugging
mlbb-automation run --config config.yaml --step google_account
```

## Configuration

All settings live in `config.yaml` (see `config.example.yaml`). Every field can also be overridden with an environment variable prefixed `MLBB_`:

| Setting | Env variable | Required | Default | Description |
|---------|-------------|----------|---------|-------------|
| `selectel_api_key` | `MLBB_SELECTEL_API_KEY` | Yes | — | Selectel Mobile Farm API key |
| `selectel_api_url` | `MLBB_SELECTEL_API_URL` | No | `https://mf.selectel.ru/api/v1` | Farm API base URL |
| `google_email` | `MLBB_GOOGLE_EMAIL` | Yes | — | Google account email (no 2FA) |
| `google_password` | `MLBB_GOOGLE_PASSWORD` | Yes | — | Google account password |
| `payment_pin` | `MLBB_PAYMENT_PIN` | No | `null` | Device unlock PIN for Google Pay auth |
| `device_filter.platform_version` | `MLBB_DEVICE_FILTER__PLATFORM_VERSION` | No | `null` | Android version filter (e.g. `"12"`) |
| `device_filter.device_model` | `MLBB_DEVICE_FILTER__DEVICE_MODEL` | No | `null` | Device model filter (e.g. `"Samsung"`) |
| `retry_count` | `MLBB_RETRY_COUNT` | No | `3` | Max retries per step |
| `retry_delay_seconds` | `MLBB_RETRY_DELAY_SECONDS` | No | `2.0` | Delay between retries (seconds) |
| `action_timeout_seconds` | `MLBB_ACTION_TIMEOUT_SECONDS` | No | `30` | Per-action timeout |
| `session_timeout_minutes` | `MLBB_SESSION_TIMEOUT_MINUTES` | No | `60` | Total session timeout |
| `log_dir` | `MLBB_LOG_DIR` | No | `./artifacts` | Directory for run reports and screenshots |
| `log_level` | `MLBB_LOG_LEVEL` | No | `INFO` | Log level: DEBUG / INFO / WARNING / ERROR |
| `save_screenshots_on_error` | `MLBB_SAVE_SCREENSHOTS_ON_ERROR` | No | `true` | Save screenshot on any failure |
| `save_all_screenshots` | `MLBB_SAVE_ALL_SCREENSHOTS` | No | `false` | Screenshot after every action (verbose) |

### About `payment_pin`

Google Pay on Android may prompt for a device unlock PIN (or biometric) to confirm a payment. Set `payment_pin` to the 4-6 digit PIN of the reserved device. If not set, any biometric or PIN prompts are cancelled and the flow continues — suitable for test environments where auth is skipped automatically.

## Package structure

```
mlbb_automation/
├── config/
│   └── settings.py         # Pydantic v2 config (YAML + env vars)
├── device_farm/
│   ├── base.py             # Abstract DeviceFarmClient interface
│   └── selectel_client.py  # Selectel Mobile Farm REST API client
├── actions/
│   └── executor.py         # Appium W3C Actions API wrapper
├── cv/
│   ├── ocr.py              # EasyOCR-based OCR engine
│   ├── template_matcher.py # Multi-scale OpenCV template matching
│   ├── screen_detector.py  # 11-state MLBB screen classifier
│   └── state_machine.py    # BFS navigation between screen states
├── logging/
│   └── logger.py           # Structlog JSON logger + RunLogger artifacts
├── recovery/
│   └── manager.py          # Freeze detection + auto app relaunch
├── scenarios/
│   ├── engine.py           # ScenarioRunner: retry + recovery orchestration
│   ├── watchdog.py         # Background popup dismisser (dialogs, ad banners)
│   └── steps/
│       ├── google_account.py   # Add Google account to device
│       ├── install_mlbb.py     # Install MLBB from Play Store
│       ├── mlbb_onboarding.py  # Skip onboarding, reach main menu
│       └── payment.py          # Shop → Diamonds → Google Pay → confirm
├── templates/              # CV template images (replace with real screenshots)
└── __main__.py             # CLI entry point
scripts/
└── capture_template.py     # ADB helper to capture and crop template images
```

## CLI commands

```
mlbb-automation check    Validate credentials and Selectel API connectivity
mlbb-automation devices  List available Android devices on the farm
mlbb-automation run      Execute the full automation scenario
```

### `run` options

| Option | Description |
|--------|-------------|
| `--config PATH` | Config file path (default: `config.yaml`) |
| `--dry-run` | Navigate to payment screen but skip the final Pay tap |
| `--step NAME` | Run only one step: `google_account`, `install_mlbb`, `mlbb_onboarding`, `payment` |
| `--device-id ID` | Reserve a specific device by ID |
| `--report-dir DIR` | Override `log_dir` from config |

## Artifacts

Each run creates a directory under `<log_dir>/<run_id>/`:

```
run_artifacts/run_20240101_120000/
├── events.jsonl        # Structured JSON event log (one event per line)
├── report.json         # Final summary: success, stats, payment result
└── screenshots/
    ├── 001_120001_google_pay_sheet.png
    ├── 002_120045_error_payment.png
    └── ...
```

## Replacing template images

All 12 template images under `mlbb_automation/templates/` are 64×32 pixel placeholders. Replace them with real device screenshots before running automation.

### Using the capture script

```bash
# 1. Connect an Android device via USB and verify ADB sees it
adb devices

# 2. Start MLBB and navigate to the desired screen

# 3. Run the capture script (interactive crop mode)
python scripts/capture_template.py shop_icon

# Or provide the crop region directly (x y width height)
python scripts/capture_template.py shop_icon --crop 820 1600 120 120

# Save a full screenshot to inspect coordinates first
python scripts/capture_template.py shop_icon --no-crop --save-raw raw_screen.png
```

### Direct ADB capture

```bash
# Take a screenshot and save locally
adb exec-out screencap -p > screen.png

# Then crop manually with any image editor and save to:
# mlbb_automation/templates/<name>.png
```

### Priority order for replacements

| Priority | Template | Screen |
|----------|----------|--------|
| High | `payment_success.png` | MLBB "Purchase Successful" screen |
| High | `payment_failed.png` | MLBB payment error screen |
| Medium | `shop_icon.png` | Main menu Shop button |
| Medium | `diamonds_tab.png` | Diamonds tab in Shop |
| Medium | `buy_button.png` | Buy button on package selection |
| Medium | `google_pay_logo.png` | Google Pay logo on payment sheet |
| Low | `google_sign_in_button.png` | "Next" button during Google sign-in |
| Low | `close_button.png` | Generic close button |
| Low | `x_button.png` | × dismiss button on banners |
| Low | `dialog_ok.png` | "OK" button in dialogs |
| Low | `mlbb_loading_logo.png` | MLBB loading screen logo |
| Low | `main_menu_bg.png` | Main menu background |

> **Note:** Low-priority templates have automatic OCR fallback. The automation will function without them, just slightly slower. High and medium priority templates directly affect payment reliability.

## Retry and safety semantics

- **Before the Pay tap** — all navigation errors are `StepError` and are safe to retry.
- **After the Pay tap** — any error becomes `PaymentError` which is marked non-retriable to prevent double charges. The run aborts immediately and saves a full report.
- **Background watchdog** — auto-dismisses Android permission dialogs, MLBB update prompts, and ad banners every 2 seconds.
- **Freeze detection** — if the screen doesn't change for 30 seconds, the manager presses Back → Home → relaunches MLBB automatically.

## Extending

To support a different device farm, implement the `DeviceFarmClient` abstract class in `device_farm/base.py` and swap it in via settings.

## Running tests

```bash
cd python-automation
pip install -e ".[dev]"
pytest tests/ -v
```
