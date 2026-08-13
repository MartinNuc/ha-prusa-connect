"""Binary sensor mapping and coordinator job selection."""

from __future__ import annotations

import pytest

from custom_components.prusa_connect.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
)
from custom_components.prusa_connect.coordinator import _job_is_more_recent
from custom_components.prusa_connect.diagnostics import _redact_printer


def _values(printer: dict) -> dict:
    return {
        d.key: d.value_fn(printer)
        for d in BINARY_SENSOR_DESCRIPTIONS
        if d.exists_fn(printer)
    }


def test_idle_printer(printer):
    """A reachable, idle printer is online and not printing."""
    values = _values(printer)
    assert values["online"] is True
    assert values["printing"] is False
    assert values["attention_required"] is False


@pytest.mark.parametrize(
    ("state", "printing"), [("PRINTING", True), ("PAUSED", True), ("IDLE", False)]
)
def test_printing_states(printer, state, printing):
    """Paused counts as printing — the job is still on the bed."""
    assert _values({**printer, "printer_state": state})["printing"] is printing


@pytest.mark.parametrize("state", ["ATTENTION", "ERROR"])
def test_attention_states(printer, state):
    """States needing intervention raise the problem flag."""
    assert _values({**printer, "printer_state": state})["attention_required"] is True


def test_offline(printer):
    """An offline printer reports not-online."""
    assert _values({**printer, "printer_state": "OFFLINE"})["online"] is False


def test_enclosure_reads_presence(printer):
    """Enclosure presence is nested, not a top-level boolean."""
    assert _values({**printer, "enclosure": {"present": True}})["enclosure"] is True
    assert _values({**printer, "enclosure": {"present": False}})["enclosure"] is False


def test_enclosure_absent_creates_no_entity(printer):
    """Printers without the field get no enclosure entity."""
    without = {k: v for k, v in printer.items() if k != "enclosure"}
    assert "enclosure" not in _values(without)


def test_active_job_beats_finished(job):
    """A running print outranks a completed one, even if it started earlier."""
    active = {**job, "state": "PRINTING", "start": job["start"] - 10_000}
    assert _job_is_more_recent(active, job) is True
    assert _job_is_more_recent(job, active) is False


def test_newer_job_wins_when_both_finished(job):
    """Otherwise the most recently started job is the current one."""
    older = {**job, "start": job["start"] - 10_000}
    assert _job_is_more_recent(job, older) is True


def test_diagnostics_redacts_credentials():
    """Diagnostics must not leak printer credentials or network details."""
    printer = {
        "name": "CORE One",
        "api_key": "SECRET",
        "prusaconnect_api_key": "SECRET",
        "prusalink_api_key": "SECRET",
        "sn": "SECRET",
        "network_info": {"wifi_ssid": "SECRET"},
    }
    redacted = _redact_printer(printer)

    assert "SECRET" not in str(redacted)
    assert redacted["name"] == "CORE One"
