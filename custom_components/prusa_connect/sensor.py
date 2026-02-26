"""Sensor platform for Prusa Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import PrusaConnectConfigEntry
from .const import DATA_JOB_COORDINATOR, DATA_PRINTER_COORDINATOR, DOMAIN, JobState, PrinterState
from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity


@dataclass(frozen=True, kw_only=True)
class PrusaConnectSensorEntityDescription(SensorEntityDescription):
    """Describes a Prusa Connect sensor entity."""

    value_fn: Callable[[dict, dict | None], StateType | datetime | None]
    available_fn: Callable[[dict], bool] = lambda data: True
    exists_fn: Callable[[dict], bool] = lambda data: True


def _get_telemetry_float(
    key: str,
) -> Callable[[dict, dict | None], StateType | None]:
    """Create a value_fn that extracts a float telemetry value."""

    def _fn(printer: dict, job: dict | None) -> StateType | None:
        telemetry = printer.get("telemetry") or {}
        val = telemetry.get(key)
        if val is None:
            return None
        try:
            return round(float(val), 1)
        except (ValueError, TypeError):
            return None

    return _fn


def _get_telemetry_int(
    key: str,
) -> Callable[[dict, dict | None], StateType | None]:
    """Create a value_fn that extracts an integer telemetry value."""

    def _fn(printer: dict, job: dict | None) -> StateType | None:
        telemetry = printer.get("telemetry") or {}
        val = telemetry.get(key)
        if val is None:
            return None
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None

    return _fn


def _compute_time_remaining(printer: dict, job: dict | None) -> StateType | None:
    """Compute time remaining for current job in seconds."""
    if not job:
        return None
    # Try direct field first
    remaining = job.get("timeRemaining")
    if remaining is not None:
        return int(remaining)
    # Compute from estimated end and current time
    end_str = job.get("estimatedEnd")
    if end_str:
        try:
            end = datetime.fromisoformat(end_str)
            now = datetime.now(timezone.utc)
            remaining_secs = (end - now).total_seconds()
            return max(0, int(remaining_secs))
        except (ValueError, TypeError):
            pass
    return None


def _compute_time_elapsed(printer: dict, job: dict | None) -> StateType | None:
    """Compute time elapsed for current job in seconds."""
    if not job:
        return None
    started = job.get("startedAt")
    if not started:
        return None
    try:
        start = datetime.fromisoformat(started)
        now = datetime.now(timezone.utc)
        return max(0, int((now - start).total_seconds()))
    except (ValueError, TypeError):
        return None


SENSOR_DESCRIPTIONS: tuple[PrusaConnectSensorEntityDescription, ...] = (
    PrusaConnectSensorEntityDescription(
        key="state",
        translation_key="state",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in PrinterState],
        value_fn=lambda p, j: p.get("state", PrinterState.UNKNOWN),
    ),
    PrusaConnectSensorEntityDescription(
        key="nozzle_temperature",
        translation_key="nozzle_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_get_telemetry_float("temp_nozzle"),
        available_fn=lambda p: p.get("state") != "OFFLINE",
    ),
    PrusaConnectSensorEntityDescription(
        key="nozzle_target_temperature",
        translation_key="nozzle_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        value_fn=_get_telemetry_float("target_nozzle"),
        available_fn=lambda p: p.get("state") != "OFFLINE",
    ),
    PrusaConnectSensorEntityDescription(
        key="bed_temperature",
        translation_key="bed_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_get_telemetry_float("temp_bed"),
        available_fn=lambda p: p.get("state") != "OFFLINE",
    ),
    PrusaConnectSensorEntityDescription(
        key="bed_target_temperature",
        translation_key="bed_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        value_fn=_get_telemetry_float("target_bed"),
        available_fn=lambda p: p.get("state") != "OFFLINE",
    ),
    PrusaConnectSensorEntityDescription(
        key="print_speed",
        translation_key="print_speed",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:speedometer",
        value_fn=_get_telemetry_int("printing_speed"),
        available_fn=lambda p: p.get("state") not in ("OFFLINE", "IDLE"),
    ),
    PrusaConnectSensorEntityDescription(
        key="flow_factor",
        translation_key="flow_factor",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:water-percent",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_get_telemetry_int("flow_factor"),
        available_fn=lambda p: p.get("state") not in ("OFFLINE", "IDLE"),
    ),
    PrusaConnectSensorEntityDescription(
        key="z_height",
        translation_key="z_height",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_get_telemetry_float("pos_z_mm"),
        available_fn=lambda p: p.get("state") not in ("OFFLINE", "IDLE"),
    ),
    PrusaConnectSensorEntityDescription(
        key="print_progress",
        translation_key="print_progress",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:progress-check",
        value_fn=lambda p, j: (
            round(j["progress"] * 100, 1)
            if j and j.get("progress") is not None
            else None
        ),
    ),
    PrusaConnectSensorEntityDescription(
        key="current_job",
        translation_key="current_job",
        icon="mdi:file-document",
        value_fn=lambda p, j: j.get("fileName") or j.get("displayName") if j else None,
    ),
    PrusaConnectSensorEntityDescription(
        key="job_state",
        translation_key="job_state",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in JobState],
        value_fn=lambda p, j: j.get("state", JobState.UNKNOWN) if j else None,
    ),
    PrusaConnectSensorEntityDescription(
        key="time_remaining",
        translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer-sand",
        value_fn=_compute_time_remaining,
    ),
    PrusaConnectSensorEntityDescription(
        key="time_elapsed",
        translation_key="time_elapsed",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer",
        value_fn=_compute_time_elapsed,
    ),
    PrusaConnectSensorEntityDescription(
        key="material",
        translation_key="material",
        icon="mdi:printer-3d-nozzle",
        value_fn=lambda p, j: p.get("material"),
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
        value_fn=lambda p, j: p.get("serialNumber"),
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

    @property
    def native_value(self) -> StateType | datetime | None:
        """Return the sensor value."""
        job = self._job_coordinator.data.get(self._printer_uuid)
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
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Prusa Connect sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    printer_coordinator: PrusaConnectPrinterCoordinator = data[DATA_PRINTER_COORDINATOR]
    job_coordinator: PrusaConnectJobCoordinator = data[DATA_JOB_COORDINATOR]

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
