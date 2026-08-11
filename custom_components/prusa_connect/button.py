"""Button platform for Prusa Connect."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PrusaConnectAPI
from .const import (
    CMD_CANCEL_READY,
    CMD_PAUSE,
    CMD_RESUME,
    CMD_SET_READY,
    CMD_STATES,
    CMD_STOP,
)
from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry


def _state(printer: dict) -> str | None:
    """Return the printer's reported state."""
    return printer.get("printer_state") or printer.get("state")


@dataclass(frozen=True, kw_only=True)
class PrusaConnectButtonEntityDescription(ButtonEntityDescription):
    """Describes a Prusa Connect button entity."""

    command: str
    press_fn: Callable[[PrusaConnectAPI, str], Coroutine[Any, Any, None]]


BUTTON_DESCRIPTIONS: tuple[PrusaConnectButtonEntityDescription, ...] = (
    PrusaConnectButtonEntityDescription(
        key="pause_print",
        translation_key="pause_print",
        icon="mdi:pause",
        command=CMD_PAUSE,
        press_fn=lambda api, uuid: api.pause_print(uuid),
    ),
    PrusaConnectButtonEntityDescription(
        key="resume_print",
        translation_key="resume_print",
        icon="mdi:play",
        command=CMD_RESUME,
        press_fn=lambda api, uuid: api.resume_print(uuid),
    ),
    PrusaConnectButtonEntityDescription(
        key="stop_print",
        translation_key="stop_print",
        icon="mdi:stop",
        command=CMD_STOP,
        press_fn=lambda api, uuid: api.stop_print(uuid),
    ),
    PrusaConnectButtonEntityDescription(
        key="set_ready",
        translation_key="set_ready",
        icon="mdi:check-circle-outline",
        command=CMD_SET_READY,
        press_fn=lambda api, uuid: api.set_ready(uuid),
    ),
    PrusaConnectButtonEntityDescription(
        key="cancel_ready",
        translation_key="cancel_ready",
        icon="mdi:close-circle-outline",
        command=CMD_CANCEL_READY,
        press_fn=lambda api, uuid: api.set_unready(uuid),
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
        self.entity_description = description
        self._api = api
        self._attr_unique_id = f"{printer_uuid}_{description.key}"

    async def async_press(self) -> None:
        """Send the command to the printer."""
        await self.entity_description.press_fn(self._api, self._printer_uuid)
        # Poll faster for a short window so the new state shows up promptly.
        self.coordinator.expect_change()
        await self.coordinator.async_request_refresh()

    @property
    def available(self) -> bool:
        """Only available in states the printer accepts the command from."""
        if not super().available:
            return False
        allowed = CMD_STATES.get(self.entity_description.command, frozenset())
        return _state(self._printer_data) in allowed


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect buttons."""
    data = entry.runtime_data
    printer_coordinator = data.printer_coordinator

    async_add_entities(
        PrusaConnectButton(printer_coordinator, data.api, printer_uuid, description)
        for printer_uuid in printer_coordinator.data
        for description in BUTTON_DESCRIPTIONS
    )
