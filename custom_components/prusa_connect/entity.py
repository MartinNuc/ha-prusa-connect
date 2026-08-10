"""Base entity for Prusa Connect."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONFIGURATION_URL, DOMAIN, MANUFACTURER
from .coordinator import PrusaConnectPrinterCoordinator


class PrusaConnectEntity(CoordinatorEntity[PrusaConnectPrinterCoordinator]):
    """Base entity for Prusa Connect."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        printer_uuid: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._printer_uuid = printer_uuid

    @property
    def _printer_data(self) -> dict:
        """Return the printer data from the coordinator."""
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get(self._printer_uuid, {})

    @property
    def _temp(self) -> dict:
        """Return the printer's temperature block."""
        return self._printer_data.get("temp") or {}

    @property
    def _job_info(self) -> dict:
        """Return live job info, present on the detail document while printing."""
        return self._printer_data.get("job_info") or {}

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this printer."""
        printer = self._printer_data
        return DeviceInfo(
            identifiers={(DOMAIN, self._printer_uuid)},
            name=printer.get("name", f"Prusa Printer {self._printer_uuid[:8]}"),
            manufacturer=MANUFACTURER,
            model=printer.get("printer_type_name") or printer.get("printer_model"),
            sw_version=printer.get("firmware"),
            serial_number=printer.get("sn"),
            configuration_url=CONFIGURATION_URL,
        )
