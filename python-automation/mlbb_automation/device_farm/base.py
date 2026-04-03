"""
Abstract base classes for device farm clients.

Defining a common interface allows swapping Selectel for another provider
(BrowserStack, AWS Device Farm, etc.) with zero changes to the rest of the code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DeviceInfo:
    """Describes a device available in the farm."""

    id: str
    name: str
    platform: str          # e.g. "Android"
    platform_version: str  # e.g. "12"
    model: str             # e.g. "Samsung Galaxy S21"
    status: str            # e.g. "available", "busy", "offline"
    udid: Optional[str] = None
    raw: dict = field(default_factory=dict)  # original API response payload


@dataclass
class ReservedDevice:
    """
    Represents a successfully reserved device.

    Contains everything needed to start an Appium session.
    Selectel Mobile Farm devices are accessed via ADB over TCP:
        adb connect <adb_host>:<adb_port>
    The public ADB key must be registered in Selectel before connecting.
    """

    device_info: DeviceInfo
    appium_url: str
    capabilities: dict  # Appium desiredCapabilities / options dict
    session_id: Optional[str] = None  # farm-level session/reservation ID
    adb_host: Optional[str] = None   # e.g. "adb.mobfarm.selectel.ru"
    adb_port: Optional[int] = None   # TCP port from rent/start response


class DeviceFarmClient(ABC):
    """Abstract interface for any mobile device farm."""

    @abstractmethod
    def list_devices(
        self,
        platform_version: Optional[str] = None,
        model: Optional[str] = None,
    ) -> list[DeviceInfo]:
        """
        Return available (not busy) devices, optionally filtered.

        Args:
            platform_version: e.g. "12" — only return devices on this Android version.
            model:            e.g. "Samsung Galaxy S21" — filter by model name substring.
        """

    @abstractmethod
    def acquire_device_by_id(self, device_id: str) -> "ReservedDevice":
        """
        Reserve a specific device by its ID.

        Args:
            device_id: The exact device ID from list_devices().

        Raises:
            RuntimeError: If the device cannot be reserved.
        """

    @abstractmethod
    def acquire_device(
        self,
        platform_version: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ReservedDevice:
        """
        Reserve the first available device matching the filters.

        Returns:
            ReservedDevice with Appium URL and capabilities ready to use.

        Raises:
            RuntimeError: If no device is available.
        """

    @abstractmethod
    def release_device(self, reserved: ReservedDevice) -> None:
        """
        Release the previously reserved device back to the pool.

        Args:
            reserved: The ReservedDevice returned by acquire_device().
        """
