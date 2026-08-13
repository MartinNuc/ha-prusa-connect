"""Sensor mapping against payloads captured from the live API.

The integration previously read camelCase keys the API never returns, so every
sensor silently produced None. These assert the real snake_case contract.
"""

from __future__ import annotations

import pytest

from custom_components.prusa_connect.const import PrinterState
from custom_components.prusa_connect.sensor import SENSOR_DESCRIPTIONS


def _values(printer: dict, job: dict | None) -> dict:
    """Evaluate every applicable sensor for a printer."""
    return {
        d.key: d.value_fn(printer, job)
        for d in SENSOR_DESCRIPTIONS
        if d.exists_fn(printer)
    }


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("state", "IDLE"),
        ("nozzle_temperature", 28.0),
        ("nozzle_target_temperature", 0.0),
        ("bed_temperature", 26.2),
        ("bed_target_temperature", 0.0),
        ("print_speed", 100),
        ("flow_factor", 100),
        ("z_height", 2.0),
        ("material", "PLA"),
        ("firmware_version", "6.5.7+12836"),
        ("serial_number", "SN-TEST-0001"),
        ("job_state", "FIN_OK"),
        ("time_elapsed", 1561),
    ],
)
def test_sensor_values(printer, job, key, expected):
    """Each sensor reads the field the API actually returns."""
    assert _values(printer, job)[key] == expected


def test_current_job_uses_display_name(printer, job):
    """The job name comes from the file's display name."""
    assert _values(printer, job)["current_job"] == job["file"]["display_name"]


def test_no_sensor_silently_returns_none(printer, job):
    """Regression: camelCase keys made every sensor None."""
    missing = [k for k, v in _values(printer, job).items() if v is None]
    assert missing == []


def test_progress_is_clamped(printer, job):
    """A job that overran its estimate must not exceed 100%."""
    assert _values(printer, job)["print_progress"] == 100.0


def test_time_remaining_never_negative(printer, job):
    """Elapsed beyond the estimate yields zero, not a negative duration."""
    assert _values(printer, job)["time_remaining"] == 0


def test_live_job_info_wins_over_estimate(printer, job):
    """While printing, Connect's own figures take precedence."""
    printing = {**printer, "job_info": {"progress": 42, "time_remaining": 600,
                                        "time_printing": 120}}
    values = _values(printing, job)
    assert values["print_progress"] == 42
    assert values["time_remaining"] == 600
    assert values["time_elapsed"] == 120


def test_fractional_progress_is_scaled(printer, job):
    """Connect reports progress as either a fraction or a percentage."""
    printing = {**printer, "job_info": {"progress": 0.25}}
    assert _values(printing, job)["print_progress"] == 25.0


def test_unknown_state_is_normalised(printer, job):
    """An unrecognised state maps to UNKNOWN so the enum stays valid."""
    odd = {**printer, "printer_state": "SOMETHING_NEW"}
    assert _values(odd, job)["state"] == PrinterState.UNKNOWN.value


def test_state_options_cover_the_api(printer, job, supported_commands):
    """Every state the API can issue commands from must be a known option."""
    options = {s.value for s in PrinterState}
    api_states = {
        state
        for command in supported_commands
        for state in command["executable_from_state"]
    }
    assert api_states <= options


def test_sensors_unavailable_when_offline(printer):
    """Temperatures are meaningless for an offline printer."""
    offline = {**printer, "printer_state": "OFFLINE"}
    temp = next(d for d in SENSOR_DESCRIPTIONS if d.key == "nozzle_temperature")
    assert temp.available_fn(offline) is False
    assert temp.available_fn(printer) is True


def test_missing_job_yields_no_job_sensors(printer):
    """With no job, job-derived sensors are None rather than raising."""
    values = _values(printer, None)
    for key in ("current_job", "job_state", "print_progress", "time_remaining"):
        assert values[key] is None
