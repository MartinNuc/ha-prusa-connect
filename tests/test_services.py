"""Tests for Prusa Connect services."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect.const import DOMAIN

from .conftest import MOCK_PRINTER_UUID


def _get_device_id(hass: HomeAssistant) -> str:
    """Look up the device id for the mock printer."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, MOCK_PRINTER_UUID)})
    assert device is not None
    return device.id


async def test_pause_print_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the pause_print service calls the API correctly."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "pause_print",
        {"device_id": device_id},
        blocking=True,
    )

    mock_api.pause_print.assert_called_once_with(MOCK_PRINTER_UUID)


async def test_resume_print_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the resume_print service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "resume_print",
        {"device_id": device_id},
        blocking=True,
    )

    mock_api.resume_print.assert_called_once_with(MOCK_PRINTER_UUID)


async def test_stop_print_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the stop_print service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "stop_print",
        {"device_id": device_id},
        blocking=True,
    )

    mock_api.stop_print.assert_called_once_with(MOCK_PRINTER_UUID)


async def test_start_print_cloud_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the start_print_cloud service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "start_print_cloud",
        {
            "device_id": device_id,
            "file_hash": "abc123",
            "team_id": 1,
        },
        blocking=True,
    )

    mock_api.start_print_cloud.assert_called_once_with(
        MOCK_PRINTER_UUID, "abc123", 1
    )


async def test_start_print_usb_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the start_print_usb service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "start_print_usb",
        {
            "device_id": device_id,
            "path": "/usb/model.gcode",
        },
        blocking=True,
    )

    mock_api.start_print_usb.assert_called_once_with(
        MOCK_PRINTER_UUID, "/usb/model.gcode"
    )


async def test_start_print_url_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the start_print_url service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "start_print_url",
        {
            "device_id": device_id,
            "file_url": "https://printables.com/file.gcode",
        },
        blocking=True,
    )

    mock_api.start_print_url.assert_called_once_with(
        MOCK_PRINTER_UUID, "https://printables.com/file.gcode"
    )


async def test_set_ready_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the set_ready service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "set_ready",
        {"device_id": device_id},
        blocking=True,
    )

    mock_api.set_ready.assert_called_once_with(MOCK_PRINTER_UUID)


async def test_cancel_ready_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the cancel_ready service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "cancel_ready",
        {"device_id": device_id},
        blocking=True,
    )

    mock_api.set_unready.assert_called_once_with(MOCK_PRINTER_UUID)


async def test_respond_to_dialog_service(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test the respond_to_dialog service."""
    device_id = _get_device_id(hass)

    await hass.services.async_call(
        DOMAIN,
        "respond_to_dialog",
        {
            "device_id": device_id,
            "dialog_id": 7,
            "button": "OK",
        },
        blocking=True,
    )

    mock_api.respond_to_dialog.assert_called_once_with(
        MOCK_PRINTER_UUID, 7, "OK"
    )
