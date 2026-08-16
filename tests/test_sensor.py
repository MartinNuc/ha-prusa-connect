"""Sensor mapping against payloads captured from the live API.

The integration previously read camelCase keys the API never returns, so every
sensor silently produced None. These assert the real snake_case contract.
"""

from __future__ import annotations

import time

import pytest

from custom_components.prusa_connect.const import PrinterState
from custom_components.prusa_connect.sensor import (
    SENSOR_DESCRIPTIONS,
    _hours_minutes,
)


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


class TestOfflinePrinter:
    """`printer_state` does not notice a printer going away; `connect_state` does."""

    def test_state_reports_offline(self, printer):
        """The observed case: unplugged overnight, still reading FINISHED."""
        gone = {**printer, "printer_state": "FINISHED", "connect_state": "OFFLINE"}
        assert _values(gone, None)["state"] == "OFFLINE"

    def test_a_stale_print_state_does_not_win(self, printer):
        gone = {**printer, "printer_state": "PRINTING", "connect_state": "OFFLINE"}
        assert _values(gone, None)["state"] == "OFFLINE"

    def test_an_online_printer_is_untouched(self, printer):
        live = {**printer, "printer_state": "PRINTING", "connect_state": "PRINTING"}
        assert _values(live, None)["state"] == "PRINTING"

    def test_a_missing_connect_state_does_not_mean_offline(self, printer):
        """Absence is not evidence — only the literal OFFLINE counts."""
        without = {k: v for k, v in printer.items() if k != "connect_state"}
        assert _values(without, None)["state"] == "IDLE"

    def test_the_captured_payload_really_carries_it(self, printer):
        """Guards the premise: this fix is worthless if the field is not there."""
        assert printer["connect_state"] == "IDLE"

    def test_the_state_sensor_stays_available_when_offline(self, printer):
        """It is the one entity whose job is to announce the printer is gone."""
        state = next(d for d in SENSOR_DESCRIPTIONS if d.key == "state")
        gone = {**printer, "connect_state": "OFFLINE"}
        assert state.offline_fn(gone) is True

    def test_other_sensors_do_not_stay_available_when_offline(self, printer):
        """A temperature from an unreachable printer is a stale reading."""
        gone = {**printer, "connect_state": "OFFLINE"}
        for key in ("nozzle_temperature", "bed_temperature", "print_progress"):
            description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
            assert description.offline_fn(gone) is False, key


class TestElapsedWithoutReportedTime:
    """Connect does not always report how long a print has been running.

    Observed live: an active job carrying no ``time_printing`` key at all, and
    a ``job_info`` of ``{origin_id, state, print_height}``. Elapsed, remaining
    and progress all derive from this, so all three read unknown for an entire
    eight-hour print.
    """

    def _live_job(self, seconds_ago: int, state: str = "PRINTING") -> dict:
        return {
            "state": state,
            "start": time.time() - seconds_ago,
            "file": {"meta": {"estimated_print_time": 30426}},
        }

    def test_falls_back_to_the_clock(self, printer):
        printing = {**printer, "job_info": {"state": "PRINTING", "print_height": 46}}
        elapsed = _values(printing, self._live_job(3600))["time_elapsed"]
        assert 3595 <= elapsed <= 3605

    def test_remaining_recovers_too(self, printer):
        """It derives from elapsed, so it was collateral damage."""
        printing = {**printer, "job_info": {"state": "PRINTING", "print_height": 46}}
        values = _values(printing, self._live_job(3600))
        assert 26820 <= values["time_remaining"] <= 26830
        assert values["time_remaining_hm"] == "7:27"

    def test_a_reported_time_still_wins(self, printer):
        """The clock overcounts a paused print, so it is only a fallback."""
        printing = {**printer, "job_info": {"time_printing": 120}}
        assert _values(printing, self._live_job(3600))["time_elapsed"] == 120

    def test_the_jobs_own_figure_still_wins(self, printer):
        job = {**self._live_job(3600), "time_printing": 250}
        printing = {**printer, "job_info": {"state": "PRINTING"}}
        assert _values(printing, job)["time_elapsed"] == 250

    def test_a_finished_job_does_not_keep_counting(self, printer):
        """Otherwise a completed print would tick upward forever."""
        job = self._live_job(3600, state="FIN_OK")
        printing = {**printer, "job_info": {"state": "FIN_OK"}}
        assert _values(printing, job)["time_elapsed"] is None

    def test_a_job_with_no_start_is_not_guessed_at(self, printer):
        job = {"state": "PRINTING", "file": {"meta": {"estimated_print_time": 30426}}}
        printing = {**printer, "job_info": {"state": "PRINTING"}}
        assert _values(printing, job)["time_elapsed"] is None


class TestReadableDurations:
    """h:mm alongside the seconds, which stay for graphs and automations."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0:00"),
            (59, "0:00"),
            (60, "0:01"),
            (3600, "1:00"),
            (23813, "6:36"),
            (7020, "1:57"),
        ],
    )
    def test_formats_as_hours_and_minutes(self, seconds, expected):
        assert _hours_minutes(seconds) == expected

    def test_hours_are_not_wrapped_at_a_day(self):
        """Reading "3:30" for a two-day print would be worse than seconds."""
        assert _hours_minutes(2 * 86400 + 3 * 3600 + 30 * 60) == "51:30"

    @pytest.mark.parametrize("value", [None, "", "abc"])
    def test_unusable_values_give_nothing(self, value):
        assert _hours_minutes(value) is None

    def test_negative_is_clamped(self):
        """A stale estimate must not render as -1:00."""
        assert _hours_minutes(-3600) == "0:00"

    def test_reads_the_same_source_as_the_numeric_sensor(self, printer, job):
        values = _values(printer, job)
        assert values["time_elapsed"] == 1561
        assert values["time_elapsed_hm"] == "0:26"

    def test_the_numeric_sensors_survive(self, printer, job):
        """They feed history and statistics; the h:mm ones are additional."""
        values = _values(printer, job)
        for key in ("time_elapsed", "time_remaining"):
            assert key in values, f"{key} disappeared"
            assert f"{key}_hm" in values


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
