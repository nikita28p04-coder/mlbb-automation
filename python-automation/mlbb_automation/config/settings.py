"""
Configuration module using Pydantic v2.

Loads settings from a YAML file and/or environment variables.
Environment variables override YAML values and follow the prefix MLBB_*.

Example env overrides:
    MLBB_SELECTEL_API_KEY=abc123
    MLBB_GOOGLE_EMAIL=test@gmail.com
    MLBB_GOOGLE_PASSWORD=secret
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeviceFilter(BaseModel):
    """Preferences for selecting a device from the farm."""

    platform_version: Optional[str] = None
    device_model: Optional[str] = None


class Settings(BaseSettings):
    """
    Full application configuration.

    Priority (highest to lowest):
        1. Environment variables (prefix: MLBB_)
        2. Values from config YAML file
        3. Defaults defined here
    """

    model_config = SettingsConfigDict(
        env_prefix="MLBB_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Selectel Mobile Farm (IAM auth) ---
    # Mobile Farm requires IAM tokens (X-Auth-Token), NOT static API keys.
    # Docs: https://docs.selectel.ru/api/authorization/
    selectel_username: str = Field(
        ...,
        description=(
            "Selectel service user name. "
            "Find it in: Control panel → Account → Users → Service Users."
        ),
    )
    selectel_account_id: str = Field(
        ...,
        description=(
            "Selectel numeric account ID (shown in top-right of control panel). "
            "Used as the 'domain name' in Keystone auth requests."
        ),
    )
    selectel_password: str = Field(
        ...,
        description="Selectel service user password.",
    )
    selectel_api_url: str = Field(
        default="https://mf.selectel.ru/api/v1",
        description="Base URL of the Selectel Mobile Farm API.",
    )

    # --- Google account (no 2FA) ---
    google_email: str = Field(..., description="Google account email")
    google_password: str = Field(..., description="Google account password")

    # --- Device payment PIN (optional) ---
    payment_pin: Optional[str] = Field(
        default=None,
        description=(
            "Device unlock PIN for Google Pay authentication. "
            "Required only if the device shows a PIN prompt after tapping Pay. "
            "If not set, biometric/PIN prompts are cancelled and the flow continues."
        ),
    )

    # --- Device preferences ---
    device_filter: DeviceFilter = Field(default_factory=DeviceFilter)

    # --- Proxy (for accessing Selectel Farm from non-RU IPs) ---
    proxy_url: Optional[str] = Field(
        default=None,
        description=(
            "HTTPS/SOCKS5 proxy URL used for all Selectel API requests. "
            "Required when running outside Russia (geo-blocked). "
            "Examples: "
            "  http://user:pass@proxy.example.com:8080 "
            "  socks5://user:pass@proxy.example.com:1080 "
            "Can also be set via env: MLBB_PROXY_URL"
        ),
    )

    # --- Appium ---
    appium_url: Optional[str] = Field(
        default=None,
        description="Custom Appium URL. If None, obtained from the farm API.",
    )

    # --- Retry / timeouts ---
    retry_count: int = Field(default=3, ge=1, le=10)
    retry_delay_seconds: float = Field(default=2.0, ge=0.5)
    action_timeout_seconds: int = Field(default=30, ge=5)
    session_timeout_minutes: int = Field(default=60, ge=5)

    # --- Logging & artifacts ---
    log_dir: Path = Field(default=Path("./artifacts"))
    log_level: str = Field(default="INFO")
    save_screenshots_on_error: bool = True
    save_all_screenshots: bool = False

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got '{v}'")
        return upper

    @field_validator("log_dir", mode="before")
    @classmethod
    def coerce_path(cls, v: object) -> Path:
        return Path(str(v))


def load_settings(config_path: Optional[str | Path] = None) -> Settings:
    """
    Load settings from an optional YAML config file, then apply env-variable overrides.

    Args:
        config_path: Path to a YAML configuration file. Falls back to
                     the MLBB_CONFIG_PATH env variable, then 'config.yaml'
                     in the current working directory.

    Returns:
        A fully validated Settings instance.
    """
    # Resolve config file path
    if config_path is None:
        config_path = os.environ.get("MLBB_CONFIG_PATH", "config.yaml")
    config_path = Path(config_path)

    yaml_data: dict = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    # Pydantic-settings merges env vars on top of the init kwargs automatically
    return Settings(**yaml_data)
