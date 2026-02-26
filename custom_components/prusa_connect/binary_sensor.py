"""Binary sensor platform for Prusa Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PrusaConnectConfigEntry
from .const import DATA_PRINTER_COORDINATOR, DOMAIN
from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity


@dataclass(frozen=True, kw_only=True)
class PrusaConnectBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Prusa Connect binary sensor entity."""

    value_fn: Callable[[dict], bool | None]
    exists_fn: Callable[[dict], bool] = lambda data: True


BINARY_SENSOR_DESCRIPTIONS: tuple[PrusaConnectBinarySensorEntityDescription, ...] = (
    PrusaConnectBinarySensorEntityDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.get("state") != "OFFLINE",
    ),
    PrusaConnectBinarySensorEntityDescription(
        key="printing",
        translation_key="printing",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda p: p.get("state") in ("PRINTING", "PAUSED"),
    ),
    PrusaConnectBinarySensorEntityDescription(
        key="attention_required",
        translation_key="attention_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda p: p.get("state") in ("ATTENTION", "ERROR"),
    ),
    PrusaConnectBinarySensorEntityDescription(
        key="mmu_enabled",
        translation_key="mmu_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:swap-horizontal",
        value_fn=lambda p: p.get("hasMmuEnabled"),
        exists_fn=lambda p: "hasMmuEnabled" in p,
    ),
    PrusaConnectBinarySensorEntityDescription(
        key="enclosure",
        translation_key="enclosure",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cube-outline",
        value_fn=lambda p: p.get("hasEnclosure"),
        exists_fn=lambda p: "hasEnclosure" in p,
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
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Prusa Connect binary sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    printer_coordinator: PrusaConnectPrinterCoordinator = data[DATA_PRINTER_COORDINATOR]

    entities: list[PrusaConnectBinarySensor] = []

    for printer_uuid, printer_data in printer_coordinator.data.items():
        for description in BINARY_SENSOR_DESCRIPTIONS:
            if description.exists_fn(printer_data):
                entities.append(
                    PrusaConnectBinarySensor(
                        printer_coordinator,
                        printer_uuid,
                        description,
                    )
                )

    async_add_entities(entities)
