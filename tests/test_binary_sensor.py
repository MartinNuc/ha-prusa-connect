"""Tests for Prusa Connect binary sensor platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_PRINTER_DATA


async def test_binary_sensor_idle_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test binary sensor entities when printer is idle."""
    state = hass.states.get("binary_sensor.my_mk4s_online")
    assert state is not None
    assert state.state == STATE_ON  # IDLE != OFFLINE

    state = hass.states.get("binary_sensor.my_mk4s_printing")
    assert state is not None
    assert state.state == STATE_OFF

    state = hass.states.get("binary_sensor.my_mk4s_attention_required")
    assert state is not None
    assert state.state == STATE_OFF


async def test_binary_sensor_printing_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test binary sensors when printer is PRINTING."""
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

    state = hass.states.get("binary_sensor.my_mk4s_printing")
    assert state is not None
    assert state.state == STATE_ON

    state = hass.states.get("binary_sensor.my_mk4s_online")
    assert state is not None
    assert state.state == STATE_ON


async def test_binary_sensor_offline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test binary sensors when printer is OFFLINE."""
    offline_data = {**MOCK_PRINTER_DATA, "state": "OFFLINE"}
    mock_api.get_printer.return_value = offline_data

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

    state = hass.states.get("binary_sensor.my_mk4s_online")
    assert state is not None
    assert state.state == STATE_OFF


async def test_binary_sensor_attention(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test binary sensors when printer needs attention."""
    attention_data = {**MOCK_PRINTER_DATA, "state": "ATTENTION"}
    mock_api.get_printer.return_value = attention_data

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

    state = hass.states.get("binary_sensor.my_mk4s_attention_required")
    assert state is not None
    assert state.state == STATE_ON
