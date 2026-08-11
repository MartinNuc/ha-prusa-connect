"""Binary sensor platform for Prusa Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry


def _state(printer: dict) -> str | None:
    """Return the printer's reported state."""
    return printer.get("printer_state") or printer.get("state")


@dataclass(frozen=True, kw_only=True)
class PrusaConnectBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Prusa Connect binary sensor entity."""

    value_fn: Callable[[dict], bool | None]
    exists_fn: Callable[[dict], bool] = lambda data: True


BINARY_SENSOR_DESCRIPTIONS: tuple[
    PrusaConnectBinarySensorEntityDescription, ...
] = (
    PrusaConnectBinarySensorEntityDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: _state(p) != "OFFLINE",
    ),
    PrusaConnectBinarySensorEntityDescription(
        key="printing",
        translation_key="printing",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda p: _state(p) in ("PRINTING", "PAUSED"),
    ),
    PrusaConnectBinarySensorEntityDescription(
        key="attention_required",
        translation_key="attention_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda p: _state(p) in ("ATTENTION", "ERROR"),
    ),
    PrusaConnectBinarySensorEntityDescription(
        key="enclosure",
        translation_key="enclosure",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cube-outline",
        value_fn=lambda p: bool((p.get("enclosure") or {}).get("present")),
        exists_fn=lambda p: isinstance(p.get("enclosure"), dict),
    ),
)


class PrusaConnectBinarySensor(PrusaConnectEntity, BinarySensorEntity):
    """Binary sensor entity for Prusa Connect."""

    entity_description: PrusaConnectBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        printer_uuid: str,
        description: PrusaConnectBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, printer_uuid)
        self.entity_description = description
        self._attr_unique_id = f"{printer_uuid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return True if the binary sensor is on."""
        return self.entity_description.value_fn(self._printer_data)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect binary sensors."""
    printer_coordinator = entry.runtime_data.printer_coordinator

    entities: list[PrusaConnectBinarySensor] = []

    for printer_uuid, printer_data in printer_coordinator.data.items():
        for description in BINARY_SENSOR_DESCRIPTIONS:
            if description.exists_fn(printer_data):
                entities.append(
                    PrusaConnectBinarySensor(
                        printer_coordinator, printer_uuid, description
                    )
                )

    async_add_entities(entities)
