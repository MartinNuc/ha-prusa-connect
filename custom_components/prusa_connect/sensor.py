"""Sensor platform for Prusa Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import PrinterState
from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry


@dataclass(frozen=True, kw_only=True)
class PrusaConnectSensorEntityDescription(SensorEntityDescription):
    """Describes a Prusa Connect sensor entity."""

    value_fn: Callable[[dict, dict | None], StateType | datetime | None]
    available_fn: Callable[[dict], bool] = lambda data: True
    exists_fn: Callable[[dict], bool] = lambda data: True


_PRINTER_STATES = {s.value for s in PrinterState}
_OFFLINE = "OFFLINE"


def _state(printer: dict) -> str | None:
    """Return the printer state, normalising unknown values."""
    value = printer.get("printer_state") or printer.get("state")
    if value is None:
        return None
    return value if value in _PRINTER_STATES else PrinterState.UNKNOWN.value


def _temp(key: str) -> Callable[[dict, dict | None], StateType | None]:
    """Read a value from the printer's temperature block."""

    def _fn(printer: dict, job: dict | None) -> StateType | None:
        value = (printer.get("temp") or {}).get(key)
        try:
            return round(float(value), 1)
        except (ValueError, TypeError):
            return None

    return _fn


def _number(key: str) -> Callable[[dict, dict | None], StateType | None]:
    """Read a top-level numeric field from the printer document."""

    def _fn(printer: dict, job: dict | None) -> StateType | None:
        value = printer.get(key)
        try:
            return round(float(value), 2)
        except (ValueError, TypeError):
            return None

    return _fn


def _estimated_print_time(job: dict | None) -> float | None:
    """Return the slicer's estimated print time in seconds."""
    meta = ((job or {}).get("file") or {}).get("meta") or {}
    value = meta.get("estimated_print_time")
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _elapsed(printer: dict, job: dict | None) -> StateType | None:
    """Seconds spent printing the current job."""
    value = (printer.get("job_info") or {}).get("time_printing")
    if value is None:
        value = (job or {}).get("time_printing")
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _remaining(printer: dict, job: dict | None) -> StateType | None:
    """Seconds left on the current job.

    Connect reports this directly while printing; otherwise it is derived from
    the slicer estimate.
    """
    live = (printer.get("job_info") or {}).get("time_remaining")
    if live is not None:
        try:
            return max(0, int(live))
        except (ValueError, TypeError):
            pass

    estimate = _estimated_print_time(job)
    elapsed = _elapsed(printer, job)
    if estimate is None or elapsed is None:
        return None
    return max(0, int(estimate - elapsed))


def _progress(printer: dict, job: dict | None) -> StateType | None:
    """Percent complete for the current job."""
    live = (printer.get("job_info") or {}).get("progress")
    if live is not None:
        try:
            value = float(live)
            # Connect reports either a fraction or a percentage.
            return round(value * 100 if value <= 1 else value, 1)
        except (ValueError, TypeError):
            pass

    estimate = _estimated_print_time(job)
    elapsed = _elapsed(printer, job)
    if not estimate or elapsed is None:
        return None
    return round(min(elapsed / estimate * 100, 100.0), 1)


def _hours_minutes(seconds: StateType | None) -> str | None:
    """Render a number of seconds as h:mm.

    Hours are not wrapped at 24: a two-day print reading "3:30" would be
    actively misleading, so it reads "51:30".
    """
    if seconds is None:
        return None
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(total, 3600)
    return f"{hours}:{remainder // 60:02d}"


def _job_name(printer: dict, job: dict | None) -> StateType | None:
    """Human-readable name of the current job's file."""
    if not job:
        return None
    file = job.get("file") or {}
    return file.get("display_name") or file.get("name") or job.get("path")


SENSOR_DESCRIPTIONS: tuple[PrusaConnectSensorEntityDescription, ...] = (
    PrusaConnectSensorEntityDescription(
        key="state",
        translation_key="state",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in PrinterState],
        value_fn=lambda p, j: _state(p),
    ),
    PrusaConnectSensorEntityDescription(
        key="nozzle_temperature",
        translation_key="nozzle_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_temp("temp_nozzle"),
        available_fn=lambda p: _state(p) != _OFFLINE,
    ),
    PrusaConnectSensorEntityDescription(
        key="nozzle_target_temperature",
        translation_key="nozzle_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        value_fn=_temp("target_nozzle"),
        available_fn=lambda p: _state(p) != _OFFLINE,
    ),
    PrusaConnectSensorEntityDescription(
        key="bed_temperature",
        translation_key="bed_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_temp("temp_bed"),
        available_fn=lambda p: _state(p) != _OFFLINE,
    ),
    PrusaConnectSensorEntityDescription(
        key="bed_target_temperature",
        translation_key="bed_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        value_fn=_temp("target_bed"),
        available_fn=lambda p: _state(p) != _OFFLINE,
    ),
    PrusaConnectSensorEntityDescription(
        key="print_speed",
        translation_key="print_speed",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:speedometer",
        value_fn=_number("speed"),
        available_fn=lambda p: _state(p) != _OFFLINE,
    ),
    PrusaConnectSensorEntityDescription(
        key="flow_factor",
        translation_key="flow_factor",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:water-percent",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_number("flow"),
        available_fn=lambda p: _state(p) != _OFFLINE,
        exists_fn=lambda p: "flow" in p,
    ),
    PrusaConnectSensorEntityDescription(
        key="z_height",
        translation_key="z_height",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_number("axis_z"),
        available_fn=lambda p: _state(p) != _OFFLINE,
    ),
    PrusaConnectSensorEntityDescription(
        key="print_progress",
        translation_key="print_progress",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:progress-check",
        value_fn=_progress,
    ),
    PrusaConnectSensorEntityDescription(
        key="current_job",
        translation_key="current_job",
        icon="mdi:file-document",
        value_fn=_job_name,
    ),
    PrusaConnectSensorEntityDescription(
        key="job_state",
        translation_key="job_state",
        icon="mdi:printer-3d",
        value_fn=lambda p, j: (j or {}).get("state"),
    ),
    PrusaConnectSensorEntityDescription(
        key="time_remaining",
        translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer-sand",
        value_fn=_remaining,
    ),
    PrusaConnectSensorEntityDescription(
        key="time_elapsed",
        translation_key="time_elapsed",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer",
        value_fn=_elapsed,
    ),
    # Seconds are what the API reports, and what graphs, statistics and
    # automation thresholds want. "23813 s" is not a print time anyone can read
    # at a glance, though, so these two say the same thing in h:mm — alongside
    # the numeric sensors rather than replacing them.
    PrusaConnectSensorEntityDescription(
        key="time_remaining_hm",
        translation_key="time_remaining_hm",
        icon="mdi:timer-sand",
        value_fn=lambda p, j: _hours_minutes(_remaining(p, j)),
    ),
    PrusaConnectSensorEntityDescription(
        key="time_elapsed_hm",
        translation_key="time_elapsed_hm",
        icon="mdi:timer",
        value_fn=lambda p, j: _hours_minutes(_elapsed(p, j)),
    ),
    PrusaConnectSensorEntityDescription(
        key="material",
        translation_key="material",
        icon="mdi:printer-3d-nozzle",
        value_fn=lambda p, j: (p.get("filament") or {}).get("material"),
    ),
    PrusaConnectSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chip",
        value_fn=lambda p, j: p.get("firmware"),
    ),
    PrusaConnectSensorEntityDescription(
        key="serial_number",
        translation_key="serial_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:identifier",
        value_fn=lambda p, j: p.get("sn"),
        exists_fn=lambda p: bool(p.get("sn")),
    ),
)


class PrusaConnectSensor(PrusaConnectEntity, SensorEntity):
    """Sensor entity for Prusa Connect."""

    entity_description: PrusaConnectSensorEntityDescription

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        job_coordinator: PrusaConnectJobCoordinator,
        printer_uuid: str,
        description: PrusaConnectSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, printer_uuid)
        self.entity_description = description
        self._job_coordinator = job_coordinator
        self._attr_unique_id = f"{printer_uuid}_{description.key}"

    async def async_added_to_hass(self) -> None:
        """Register listeners for both coordinators."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._job_coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from either coordinator."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType | datetime | None:
        """Return the sensor value."""
        job = (
            self._job_coordinator.data.get(self._printer_uuid)
            if self._job_coordinator.data
            else None
        )
        return self.entity_description.value_fn(self._printer_data, job)

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        if not super().available:
            return False
        return self.entity_description.available_fn(self._printer_data)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect sensors."""
    data = entry.runtime_data
    printer_coordinator = data.printer_coordinator
    job_coordinator = data.job_coordinator

    entities: list[PrusaConnectSensor] = []

    for printer_uuid, printer_data in printer_coordinator.data.items():
        for description in SENSOR_DESCRIPTIONS:
            if description.exists_fn(printer_data):
                entities.append(
                    PrusaConnectSensor(
                        printer_coordinator,
                        job_coordinator,
                        printer_uuid,
                        description,
                    )
                )

    async_add_entities(entities)
