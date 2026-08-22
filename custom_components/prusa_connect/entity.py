"""Base entity for Prusa Connect."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONFIGURATION_URL,
    CONNECT_STATE_OFFLINE,
    DOMAIN,
    MANUFACTURER,
)
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
    def _is_offline(self) -> bool:
        """Whether Connect has lost contact with the printer.

        A different question from ``printer_state``, which is the last *print*
        state Connect knew about and happily persists while the printer is
        unreachable — one that dropped off the network overnight still read
        FINISHED the next morning. ``connect_state`` tracks the link itself.
        """
        return self._printer_data.get("connect_state") == CONNECT_STATE_OFFLINE

    @property
    def available(self) -> bool:
        """Whether this entity's value can be trusted right now.

        A temperature read from an unreachable printer is whatever was true
        when it was last seen, presented as though it were current. Saying
        nothing is more honest than saying something stale.
        """
        return super().available and not self._is_offline

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
