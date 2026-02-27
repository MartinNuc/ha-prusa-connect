"""Edge case tests for Prusa Connect sensor value parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_JOB_DATA, MOCK_PRINTER_DATA, MOCK_PRINTER_UUID


async def _setup_with_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
    printer_override: dict | None = None,
    job_override: dict | None = None,
) -> None:
    """Set up integration with custom printer/job data."""
    if printer_override:
        mock_api.get_printer.return_value = {
            **MOCK_PRINTER_DATA,
            **printer_override,
        }
    if job_override is not None:
        mock_api.get_jobs.return_value = (
            [job_override] if job_override else []
        )

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


async def test_progress_decimal_format(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test progress value in 0.0-1.0 format is converted to percentage."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        job_override={**MOCK_JOB_DATA, "progress": 0.75},
    )
    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "75.0"


async def test_progress_percentage_format(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test progress value in 0-100 format is used directly."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        job_override={**MOCK_JOB_DATA, "progress": 42.5},
    )
    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "42.5"


async def test_progress_zero(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test progress value of 0."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        job_override={**MOCK_JOB_DATA, "progress": 0},
    )
    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "0.0"


async def test_progress_complete(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test progress value of 100."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        job_override={**MOCK_JOB_DATA, "progress": 100},
    )
    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "100.0"


async def test_progress_string_value(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test progress with string value is handled."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        job_override={**MOCK_JOB_DATA, "progress": "55.5"},
    )
    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "55.5"


async def test_progress_null(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test progress with None value."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        job_override={**MOCK_JOB_DATA, "progress": None},
    )
    state = hass.states.get("sensor.my_mk4s_print_progress")
    assert state is not None
    assert state.state == "unknown"


async def test_missing_telemetry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test sensors when telemetry is completely absent."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        printer_override={"telemetry": None},
    )
    state = hass.states.get("sensor.my_mk4s_nozzle_temperature")
    assert state is not None
    # Sensor should be available but return unknown when no telemetry
    assert state.state == "unknown"


async def test_empty_telemetry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test sensors when telemetry is an empty dict."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        printer_override={"telemetry": {}},
    )
    state = hass.states.get("sensor.my_mk4s_nozzle_temperature")
    assert state is not None
    assert state.state == "unknown"


async def test_telemetry_string_values(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test that string telemetry values are parsed correctly."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        printer_override={
            "telemetry": {
                "temp_nozzle": "215.3",
                "target_nozzle": "220",
                "temp_bed": "60.1",
                "target_bed": "60",
                "printing_speed": "150",
                "flow_factor": "95",
                "pos_z_mm": "12.45",
            }
        },
    )
    state = hass.states.get("sensor.my_mk4s_nozzle_temperature")
    assert state.state == "215.3"

    state = hass.states.get("sensor.my_mk4s_nozzle_target")
    assert state.state == "220.0"

    state = hass.states.get("sensor.my_mk4s_bed_temperature")
    assert state.state == "60.1"


async def test_telemetry_numeric_values(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test that numeric (non-string) telemetry values work."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        printer_override={
            "telemetry": {
                "temp_nozzle": 215.3,
                "target_nozzle": 220,
                "temp_bed": 60.1,
                "target_bed": 60,
                "printing_speed": 150,
                "flow_factor": 95,
                "pos_z_mm": 12.45,
            }
        },
    )
    state = hass.states.get("sensor.my_mk4s_nozzle_temperature")
    assert state.state == "215.3"


async def test_telemetry_hyphenated_keys(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test that hyphenated telemetry keys (temp-nozzle) are handled."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        printer_override={
            "telemetry": {
                "temp-nozzle": "200.5",
                "target-nozzle": "210",
                "temp-bed": "55.0",
                "target-bed": "60",
            }
        },
    )
    state = hass.states.get("sensor.my_mk4s_nozzle_temperature")
    assert state.state == "200.5"

    state = hass.states.get("sensor.my_mk4s_bed_temperature")
    assert state.state == "55.0"


async def test_unknown_printer_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test that an unrecognized printer state maps to UNKNOWN."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        printer_override={"state": "NEW_STATE_FROM_FUTURE"},
    )
    state = hass.states.get("sensor.my_mk4s_state")
    assert state is not None
    assert state.state == "UNKNOWN"


async def test_time_remaining_direct_field(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test time remaining from direct timeRemaining field."""
    await _setup_with_data(
        hass,
        mock_config_entry,
        mock_api,
        job_override={**MOCK_JOB_DATA, "timeRemaining": 3600},
    )
    state = hass.states.get("sensor.my_mk4s_time_remaining")
    assert state is not None
    assert state.state == "3600"


async def test_all_printer_states(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test that all known printer states are accepted."""
    for printer_state in [
        "IDLE",
        "READY",
        "BUSY",
        "PRINTING",
        "PAUSED",
        "FINISHED",
        "STOPPED",
        "ERROR",
        "ATTENTION",
        "OFFLINE",
    ]:
        mock_api.get_printer.return_value = {
            **MOCK_PRINTER_DATA,
            "state": printer_state,
        }
        # Reinitialize for each state — just verify the data parses
        # We do this by directly calling the value function
        from custom_components.prusa_connect.sensor import SENSOR_DESCRIPTIONS

        state_desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "state")
        result = state_desc.value_fn(
            {"state": printer_state}, None
        )
        assert result == printer_state, (
            f"State {printer_state} should be accepted as-is"
        )
