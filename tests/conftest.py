"""Fixtures for Prusa Connect integration tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect.const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

MOCK_PRINTER_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

MOCK_PRINTER_DATA: dict[str, Any] = {
    "uuid": MOCK_PRINTER_UUID,
    "name": "My MK4S",
    "state": "IDLE",
    "printerType": "MK4S",
    "printerTypeName": "Original Prusa MK4S",
    "firmware": "5.1.2",
    "serialNumber": "SN-123456",
    "material": "PLA",
    "hasMmuEnabled": False,
    "hasEnclosure": False,
    "telemetry": {
        "temp_nozzle": "21.5",
        "target_nozzle": "0",
        "temp_bed": "22.3",
        "target_bed": "0",
        "printing_speed": "100",
        "flow_factor": "100",
        "pos_z_mm": "0.0",
    },
}

MOCK_JOB_DATA: dict[str, Any] = {
    "id": 42,
    "printerUuid": MOCK_PRINTER_UUID,
    "state": "PRINTING",
    "progress": 0.45,
    "fileName": "benchy.gcode",
    "displayName": "Benchy",
    "timeRemaining": 1800,
    "startedAt": "2026-02-26T10:00:00+00:00",
    "estimatedEnd": "2026-02-26T11:00:00+00:00",
    "thumbnailUrl": "https://example.com/thumb.png",
}

MOCK_USER: dict[str, Any] = {
    "id": 12345,
    "email": "test@example.com",
}

MOCK_TOKENS: dict[str, str] = {
    "access_token": "mock-access-token",
    "refresh_token": "mock-refresh-token",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable custom integrations in all tests."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry for the integration."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="test@example.com",
        unique_id="12345",
        data={
            CONF_ACCESS_TOKEN: "mock-access-token",
            CONF_REFRESH_TOKEN: "mock-refresh-token",
            CONF_USER_ID: "12345",
        },
        version=1,
    )


@pytest.fixture
def mock_api() -> AsyncMock:
    """Return a mocked PrusaConnectAPI."""
    api = AsyncMock()
    api.get_user.return_value = MOCK_USER
    api.get_printers.return_value = [
        {"uuid": MOCK_PRINTER_UUID, "name": "My MK4S"},
    ]
    api.get_printer.return_value = MOCK_PRINTER_DATA
    api.get_jobs.return_value = [MOCK_JOB_DATA]
    api.get_cameras.return_value = []

    # Command methods return None
    api.pause_print.return_value = None
    api.resume_print.return_value = None
    api.stop_print.return_value = None
    api.set_ready.return_value = None
    api.set_unready.return_value = None
    api.start_print_cloud.return_value = None
    api.start_print_usb.return_value = None
    api.start_print_url.return_value = None
    api.respond_to_dialog.return_value = None

    return api


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Patch async_setup_entry to return True (skip real setup)."""
    with patch(
        "custom_components.prusa_connect.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> MockConfigEntry:
    """Set up the Prusa Connect integration with mocked API."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.prusa_connect.PrusaConnectAPI",
            return_value=mock_api,
        ),
        patch(
            "custom_components.prusa_connect.async_get_clientsession",
        ),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    return mock_config_entry
