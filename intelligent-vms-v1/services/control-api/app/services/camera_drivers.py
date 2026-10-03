from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DriverDescriptor:
    """Describe one installed manufacturer-specific camera driver."""

    driver_id: str
    manufacturer: str
    display_name: str
    version: str
    capabilities: tuple[str, ...] = ()


class ManufacturerDriver(Protocol):
    """Contract implemented by manufacturer-specific camera adapters."""

    descriptor: DriverDescriptor

    def matches(self, manufacturer: str, model: str | None = None) -> bool:
        """Return whether this adapter explicitly supports the named device."""
        ...


class DriverRegistry:
    """Keep an explicit registry of installed manufacturer-specific adapters."""

    def __init__(self) -> None:
        self._drivers: dict[str, ManufacturerDriver] = {}

    def register(self, driver: ManufacturerDriver) -> None:
        """Register one adapter by stable driver identifier.

        Args:
            driver: Manufacturer-specific adapter implementing the registry contract.

        Returns:
            None after successful registration.

        Raises:
            ValueError: If the descriptor is incomplete or ID already exists.
        """
        descriptor = driver.descriptor
        if not descriptor.driver_id.strip() or not descriptor.manufacturer.strip():
            raise ValueError("driver descriptor requires ID and manufacturer")
        if descriptor.driver_id in self._drivers:
            raise ValueError("driver ID is already registered")
        self._drivers[descriptor.driver_id] = driver

    def descriptors(self) -> list[DriverDescriptor]:
        """Return installed adapter descriptors in deterministic order.

        Returns:
            Installed immutable driver descriptors sorted by driver ID.
        """
        return [
            self._drivers[key].descriptor
            for key in sorted(self._drivers)
        ]

    def resolve(
        self,
        manufacturer: str,
        model: str | None = None,
    ) -> ManufacturerDriver | None:
        """Return the first explicitly matching adapter, if installed.

        Args:
            manufacturer: Device manufacturer reported by the camera.
            model: Optional device model used for adapter matching.

        Returns:
            Matching installed adapter, otherwise None.
        """
        for key in sorted(self._drivers):
            driver = self._drivers[key]
            if driver.matches(manufacturer, model):
                return driver
        return None


driver_registry = DriverRegistry()
