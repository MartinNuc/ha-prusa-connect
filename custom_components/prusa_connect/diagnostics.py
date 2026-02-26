"""Diagnostics support for Prusa Connect."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DATA_JOB_COORDINATOR, DATA_PRINTER_COORDINATOR, DOMAIN


def _redact_printer(printer: dict) -> dict:
    """Redact sensitive data from a printer dict."""
    redacted = dict(printer)
    for key in ("serialNumber", "apiKey", "token"):
        if key in redacted:
            redacted[key] = "**REDACTED**"

    # Redact snapshot URLs (contain signed tokens)
    for key in ("snapshotUrl", "cameraUrl"):
        if key in redacted and redacted[key]:
            redacted[key] = "**REDACTED_URL**"

    # Redact camera URLs
    cameras = redacted.get("cameras")
    if cameras:
        redacted_cameras = []
        for cam in cameras:
            rc = dict(cam)
            for url_key in ("snapshotUrl", "imageUrl", "streamUrl"):
                if url_key in rc and rc[url_key]:
                    rc[url_key] = "**REDACTED_URL**"
            redacted_cameras.append(rc)
        redacted["cameras"] = redacted_cameras

    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    printer_coordinator = data.get(DATA_PRINTER_COORDINATOR)
    job_coordinator = data.get(DATA_JOB_COORDINATOR)

    printers = {}
    if printer_coordinator and printer_coordinator.data:
        for uuid, printer in printer_coordinator.data.items():
            printers[uuid] = _redact_printer(printer)

    jobs = {}
    if job_coordinator and job_coordinator.data:
        for uuid, job in job_coordinator.data.items():
            redacted_job = dict(job)
            if "thumbnailUrl" in redacted_job and redacted_job["thumbnailUrl"]:
                redacted_job["thumbnailUrl"] = "**REDACTED_URL**"
            jobs[uuid] = redacted_job

    return {
        "printers": printers,
        "jobs": jobs,
    }
