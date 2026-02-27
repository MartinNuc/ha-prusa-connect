"""Tests for Prusa Connect sensor platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_PRINTER_DATA, MOCK_PRINTER_UUID


async def test_sensor_entities_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test that sensor entities are created with correct state."""
    state = hass.states.get("sensor.my_mk4s_state")
    assert state is not None
    assert state.state == "IDLE"

    state = hass.states.get("sensor.my_mk4s_nozzle_temperature")
    assert state is not None
    assert state.state == "21.5"

    state = hass.states.get("sensor.my_mk4s_bed_temperature")
    assert state is not None
    assert state.state == "22.3"

    state = hass.states.get("sensor.my_mk4s_material")
    assert state is not None
    assert state.state == "PLA"

    state = hass.states.get("sensor.my_mk4s_firmware")
    assert state is not None
    assert state.state == "5.1.2"


async def test_job_sensors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test that job-related sensors show correct values."""
    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "45.0"

    state = hass.states.get("sensor.my_mk4s_current_job")
    assert state is not None
    assert state.state == "benchy.gcode"

    state = hass.states.get("sensor.my_mk4s_job_state")
    assert state is not None
    assert state.state == "PRINTING"


async def test_sensor_unavailable_when_offline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test that temperature sensors become unavailable when offline."""
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

    state = hass.states.get("sensor.my_mk4s_nozzle_temperature")
    assert state is not None
    assert state.state == "unavailable"

    # State sensor should still be available
    state = hass.states.get("sensor.my_mk4s_state")
    assert state is not None
    assert state.state == "OFFLINE"


async def test_sensor_no_job_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test sensor values when no job data is available."""
    mock_api.get_jobs.return_value = []

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

    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "unknown"

    state = hass.states.get("sensor.my_mk4s_current_job")
    assert state is not None
    assert state.state == "unknown"
