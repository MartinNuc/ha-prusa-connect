"""Service handlers for Prusa Connect."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .api import PrusaConnectAPI
from .const import DOMAIN
from .coordinator import PrusaConnectPrinterCoordinator

_LOGGER = logging.getLogger(__name__)


def _resolve_printer(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[PrusaConnectAPI, PrusaConnectPrinterCoordinator, str]:
    """Resolve a service call's device target to (api, coordinator, uuid)."""
    device_ids = call.data.get("device_id", [])
    if isinstance(device_ids, str):
        device_ids = [device_ids]
    if not device_ids:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_device",
        )

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_ids[0])
    if not device:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
        )

    printer_uuid = None
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN:
            printer_uuid = identifier[1]
            break

    if not printer_uuid:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_prusa_device",
        )

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN:
            data = entry.runtime_data
            return data.api, data.printer_coordinator, printer_uuid

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="entry_not_found",
    )


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up Prusa Connect services."""

    if hass.services.has_service(DOMAIN, "pause_print"):
        return

    def _make_simple_handler(method_name: str):
        """Build a handler for a command that takes no arguments."""

        async def _handler(call: ServiceCall) -> None:
            api, coordinator, uuid = _resolve_printer(hass, call)
            await getattr(api, method_name)(uuid)
            coordinator.expect_change()
            await coordinator.async_request_refresh()

        return _handler

    for name, method in (
        ("pause_print", "pause_print"),
        ("resume_print", "resume_print"),
        ("stop_print", "stop_print"),
        ("set_ready", "set_ready"),
        ("cancel_ready", "set_unready"),
    ):
        hass.services.async_register(DOMAIN, name, _make_simple_handler(method))

    async def _handle_start_print(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.start_print(uuid, call.data["path"])
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "start_print",
        _handle_start_print,
        schema=vol.Schema(
            {vol.Required("path"): str},
            extra=vol.ALLOW_EXTRA,
        ),
    )

    async def _handle_respond_dialog(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.respond_to_dialog(
            uuid, int(call.data["dialog_id"]), call.data["button"]
        )
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "respond_to_dialog",
        _handle_respond_dialog,
        schema=vol.Schema(
            {
                vol.Required("dialog_id"): vol.Coerce(int),
                vol.Required("button"): str,
            },
            extra=vol.ALLOW_EXTRA,
        ),
    )
