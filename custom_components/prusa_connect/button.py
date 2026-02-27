"""Button platform for Prusa Connect."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PrusaConnectAPI
from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry


@dataclass(frozen=True, kw_only=True)
class PrusaConnectButtonEntityDescription(ButtonEntityDescription):
    """Describes a Prusa Connect button entity."""

    press_fn: Callable[[PrusaConnectAPI, str], Awaitable[None]]
    available_fn: Callable[[dict], bool]


BUTTON_DESCRIPTIONS: tuple[PrusaConnectButtonEntityDescription, ...] = (
    PrusaConnectButtonEntityDescription(
        key="pause_print",
        translation_key="pause_print",
        icon="mdi:pause",
        press_fn=lambda api, uuid: api.pause_print(uuid),
        available_fn=lambda p: p.get("state") == "PRINTING",
    ),
    PrusaConnectButtonEntityDescription(
        key="resume_print",
        translation_key="resume_print",
        icon="mdi:play",
        press_fn=lambda api, uuid: api.resume_print(uuid),
        available_fn=lambda p: p.get("state") == "PAUSED",
    ),
    PrusaConnectButtonEntityDescription(
        key="stop_print",
        translation_key="stop_print",
        icon="mdi:stop",
        press_fn=lambda api, uuid: api.stop_print(uuid),
        available_fn=lambda p: p.get("state") in ("PRINTING", "PAUSED"),
    ),
    PrusaConnectButtonEntityDescription(
        key="set_ready",
        translation_key="set_ready",
        icon="mdi:check-circle",
        press_fn=lambda api, uuid: api.set_ready(uuid),
        available_fn=lambda p: p.get("state") == "FINISHED",
    ),
    PrusaConnectButtonEntityDescription(
        key="cancel_ready",
        translation_key="cancel_ready",
        icon="mdi:close-circle",
        press_fn=lambda api, uuid: api.set_unready(uuid),
        available_fn=lambda p: p.get("state") == "READY",
    ),
)


class PrusaConnectButton(PrusaConnectEntity, ButtonEntity):
    """Button entity for Prusa Connect."""

    entity_description: PrusaConnectButtonEntityDescription

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        api: PrusaConnectAPI,
        printer_uuid: str,
        description: PrusaConnectButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, printer_uuid)
        self._api = api
        self.entity_description = description
        self._attr_unique_id = f"{printer_uuid}_{description.key}"

    @property
    def available(self) -> bool:
        """Return True if the button action is available."""
        if not super().available:
            return False
        return self.entity_description.available_fn(self._printer_data)

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.entity_description.press_fn(self._api, self._printer_uuid)
        self.coordinator.expect_change()
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect buttons."""
    data = entry.runtime_data
    printer_coordinator = data.printer_coordinator
    api = data.api

    entities: list[PrusaConnectButton] = []

    for printer_uuid in printer_coordinator.data:
        for description in BUTTON_DESCRIPTIONS:
            entities.append(
                PrusaConnectButton(
                    printer_coordinator,
                    api,
                    printer_uuid,
                    description,
                )
            )

    async_add_entities(entities)
