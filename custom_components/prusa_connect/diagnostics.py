"""Diagnostics support for Prusa Connect."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import PrusaConnectConfigEntry

# Printer documents embed live credentials and network details.
REDACT_PRINTER_KEYS = (
    "api_key",
    "prusaconnect_api_key",
    "prusalink_api_key",
    "sn",
    "network_info",
    "owner",
)

REDACT_CAMERA_KEYS = ("token", "url", "snapshot_url", "last_photo")


def _redact_printer(printer: dict) -> dict:
    """Redact sensitive data from a printer dict."""
    redacted = dict(printer)
    for key in REDACT_PRINTER_KEYS:
        if redacted.get(key) is not None:
            redacted[key] = "**REDACTED**"

    cameras = redacted.get("cameras")
    if isinstance(cameras, list):
        cleaned = []
        for cam in cameras:
            rc = dict(cam)
            for key in REDACT_CAMERA_KEYS:
                if rc.get(key) is not None:
                    rc[key] = "**REDACTED**"
            cleaned.append(rc)
        redacted["cameras"] = cleaned

    return redacted


def _redact_job(job: dict) -> dict:
    """Redact sensitive data from a job dict."""
    redacted = dict(job)
    file = redacted.get("file")
    if isinstance(file, dict):
        rf = dict(file)
        # The slicer metadata is large and adds no diagnostic value.
        rf.pop("meta", None)
        redacted["file"] = rf
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data

    printers = {
        uuid: _redact_printer(printer)
        for uuid, printer in (data.printer_coordinator.data or {}).items()
    }
    jobs = {
        uuid: _redact_job(job)
        for uuid, job in (data.job_coordinator.data or {}).items()
    }

    return {"printers": printers, "jobs": jobs}
