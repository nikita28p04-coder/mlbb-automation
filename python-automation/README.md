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

# List available devices on the farm
mlbb-automation devices --config config.yaml

# Run the full scenario (real payment)
mlbb-automation run --config config.yaml

# Dry run — navigate to payment but skip the final tap
mlbb-automation run --config config.yaml --dry-run

# Run a single step for debugging
mlbb-automation run --config config.yaml --step google_account
```

## Configuration

All settings live in `config.yaml` (see `config.example.yaml`). Every field can also be overridden with an environment variable prefixed with `MLBB_`:

```bash
export MLBB_SELECTEL_API_KEY=abc123
export MLBB_GOOGLE_EMAIL=my@gmail.com
export MLBB_GOOGLE_PASSWORD=secret
```

## Package structure

```
mlbb_automation/
├── config/
│   └── settings.py         # Pydantic v2 config (YAML + env)
├── device_farm/
│   ├── base.py             # Abstract DeviceFarmClient interface
│   └── selectel_client.py  # Selectel Mobile Farm REST API client
├── actions/
│   └── executor.py         # Appium WebDriver action wrapper
├── logging/
│   └── logger.py           # Structlog JSON logger + RunLogger artifacts
├── recovery/
│   └── manager.py          # Freeze detection + auto-recovery
├── scenarios/
│   └── steps/
│       ├── google_account.py   # Add Google account to device [stub — Task #3]
│       ├── install_mlbb.py     # Install MLBB from Play Store [stub — Task #3]
│       ├── mlbb_onboarding.py  # Skip onboarding, reach main menu [stub — Task #3]
│       └── payment.py          # Shop → Diamonds → Google Pay [stub — Task #3]
└── __main__.py             # CLI entry point

> **Note:** `cv/` (computer vision/OCR) and `scenarios/steps/` full implementation
> are Task #2 and Task #3 respectively. Scenario steps currently run as stubs
> and log `status=stub_ok`; no real device interactions happen yet. Use
> `--dry-run` to understand the flow before real steps are implemented.
```

## Artifacts

Each run creates a directory under `run_artifacts/<run_id>/`:

```
run_artifacts/run_20240101_120000/
├── events.jsonl        # Structured JSON event log (one event per line)
├── report.json         # Final summary: success, stats, payment result
└── screenshots/
    ├── 001_120001_after_login.png
    ├── 002_120045_error_payment.png
    └── ...
```

## Extending

To support a different device farm, implement the `DeviceFarmClient` abstract class in `device_farm/base.py` and swap it in via settings.
