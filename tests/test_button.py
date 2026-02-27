"""Tests for Prusa Connect button platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_PRINTER_DATA, MOCK_PRINTER_UUID


async def test_button_entities_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test that button entities are created."""
    assert hass.states.get("button.my_mk4s_pause_print") is not None
    assert hass.states.get("button.my_mk4s_resume_print") is not None
    assert hass.states.get("button.my_mk4s_stop_print") is not None
    assert hass.states.get("button.my_mk4s_set_ready") is not None
    assert hass.states.get("button.my_mk4s_cancel_ready") is not None


async def test_button_availability_idle(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test that buttons are unavailable when printer is idle."""
    state = hass.states.get("button.my_mk4s_pause_print")
    assert state is not None
    assert state.state == "unavailable"

    state = hass.states.get("button.my_mk4s_stop_print")
    assert state is not None
    assert state.state == "unavailable"


async def test_pause_button_available_when_printing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test pause button is available when printing."""
    printing_data = {**MOCK_PRINTER_DATA, "state": "PRINTING"}
    mock_api.get_printer.return_value = printing_data

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

    state = hass.states.get("button.my_mk4s_pause_print")
    assert state is not None
    assert state.state != "unavailable"


async def test_pause_button_press(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test pressing the pause button calls the API."""
    printing_data = {**MOCK_PRINTER_DATA, "state": "PRINTING"}
    mock_api.get_printer.return_value = printing_data

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

        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {"entity_id": "button.my_mk4s_pause_print"},
            blocking=True,
        )

    mock_api.pause_print.assert_called_once_with(MOCK_PRINTER_UUID)


async def test_stop_button_press(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test pressing the stop button calls the API."""
    printing_data = {**MOCK_PRINTER_DATA, "state": "PRINTING"}
    mock_api.get_printer.return_value = printing_data

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

        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {"entity_id": "button.my_mk4s_stop_print"},
            blocking=True,
        )

    mock_api.stop_print.assert_called_once_with(MOCK_PRINTER_UUID)
