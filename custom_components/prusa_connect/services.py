"""Service handlers for Prusa Connect."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .api import PrusaConnectAPI
from .const import DATA_API, DATA_PRINTER_COORDINATOR, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _resolve_printer(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[PrusaConnectAPI, str]:
    """Resolve a service call's device target to (api, printer_uuid)."""
    device_ids = call.data.get("device_id", [])
    if isinstance(device_ids, str):
        device_ids = [device_ids]
    if not device_ids:
        raise ValueError("No device specified")

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_ids[0])
    if not device:
        raise ValueError(f"Device {device_ids[0]} not found")

    # Find the printer UUID from device identifiers
    printer_uuid = None
    config_entry_id = None
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN:
            printer_uuid = identifier[1]
            break

    if not printer_uuid:
        raise ValueError(f"Device {device_ids[0]} is not a Prusa Connect printer")

    # Find the config entry for this device
    for entry_id in device.config_entries:
        if entry_id in hass.data.get(DOMAIN, {}):
            config_entry_id = entry_id
            break

    if not config_entry_id:
        raise ValueError("Config entry not found for device")

    api: PrusaConnectAPI = hass.data[DOMAIN][config_entry_id][DATA_API]
    return api, printer_uuid


def _get_coordinator(hass: HomeAssistant, call: ServiceCall):
    """Get the printer coordinator for a service call."""
    device_ids = call.data.get("device_id", [])
    if isinstance(device_ids, str):
        device_ids = [device_ids]

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_ids[0])
    if not device:
        return None

    for entry_id in device.config_entries:
        if entry_id in hass.data.get(DOMAIN, {}):
            return hass.data[DOMAIN][entry_id][DATA_PRINTER_COORDINATOR]
    return None


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up Prusa Connect services."""

    if hass.services.has_service(DOMAIN, "pause_print"):
        return

    async def _handle_pause(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.pause_print(uuid)
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_resume(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.resume_print(uuid)
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_stop(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.stop_print(uuid)
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_start_cloud(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.start_print_cloud(
            uuid,
            call.data["file_hash"],
            int(call.data["team_id"]),
        )
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_start_usb(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.start_print_usb(uuid, call.data["path"])
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_start_url(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.start_print_url(uuid, call.data["file_url"])
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_set_ready(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.set_ready(uuid)
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_cancel_ready(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.set_unready(uuid)
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    async def _handle_respond_dialog(call: ServiceCall) -> None:
        api, uuid = _resolve_printer(hass, call)
        await api.respond_to_dialog(
            uuid,
            int(call.data["dialog_id"]),
            call.data["button"],
        )
        coordinator = _get_coordinator(hass, call)
        if coordinator:
            coordinator.expect_change()
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "pause_print", _handle_pause)
    hass.services.async_register(DOMAIN, "resume_print", _handle_resume)
    hass.services.async_register(DOMAIN, "stop_print", _handle_stop)
    hass.services.async_register(DOMAIN, "start_print_cloud", _handle_start_cloud)
    hass.services.async_register(DOMAIN, "start_print_usb", _handle_start_usb)
    hass.services.async_register(DOMAIN, "start_print_url", _handle_start_url)
    hass.services.async_register(DOMAIN, "set_ready", _handle_set_ready)
    hass.services.async_register(DOMAIN, "cancel_ready", _handle_cancel_ready)
    hass.services.async_register(DOMAIN, "respond_to_dialog", _handle_respond_dialog)
