"""Service handlers for Prusa Connect."""

from __future__ import annotations

import logging
from typing import Any

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

    # Find the printer UUID from device identifiers
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

    # Find the config entry for this device
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

    async def _handle_pause(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.pause_print(uuid)
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_resume(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.resume_print(uuid)
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_stop(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.stop_print(uuid)
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_start_cloud(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.start_print_cloud(
            uuid,
            call.data["file_hash"],
            int(call.data["team_id"]),
        )
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_start_usb(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.start_print_usb(uuid, call.data["path"])
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_start_url(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.start_print_url(uuid, call.data["file_url"])
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_set_ready(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.set_ready(uuid)
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_cancel_ready(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.set_unready(uuid)
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    async def _handle_respond_dialog(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.respond_to_dialog(
            uuid,
            int(call.data["dialog_id"]),
            call.data["button"],
        )
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    # Simple services (no extra fields)
    for name, handler in (
        ("pause_print", _handle_pause),
        ("resume_print", _handle_resume),
        ("stop_print", _handle_stop),
        ("set_ready", _handle_set_ready),
        ("cancel_ready", _handle_cancel_ready),
    ):
        hass.services.async_register(DOMAIN, name, handler)

    # Services with required fields
    hass.services.async_register(
        DOMAIN,
        "start_print_cloud",
        _handle_start_cloud,
        schema=vol.Schema(
            {
                vol.Required("file_hash"): str,
                vol.Required("team_id"): vol.Coerce(int),
            },
            extra=vol.ALLOW_EXTRA,
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "start_print_usb",
        _handle_start_usb,
        schema=vol.Schema(
            {
                vol.Required("path"): str,
            },
            extra=vol.ALLOW_EXTRA,
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "start_print_url",
        _handle_start_url,
        schema=vol.Schema(
            {
                vol.Required("file_url"): str,
            },
            extra=vol.ALLOW_EXTRA,
        ),
    )
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
