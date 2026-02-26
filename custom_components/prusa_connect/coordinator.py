"""DataUpdateCoordinators for Prusa Connect."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PrusaConnectAPI
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, FAST_SCAN_DURATION, FAST_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class PrusaConnectPrinterCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Coordinator for polling printer data.

    Data shape: dict keyed by printer UUID, each value is the full printer
    detail dict (including telemetry).
    """

    def __init__(self, hass: HomeAssistant, api: PrusaConnectAPI) -> None:
        """Initialize the printer coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_printers",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self._fast_poll_until: float = 0

    def expect_change(self) -> None:
        """Speed up polling temporarily after a command."""
        self._fast_poll_until = time.monotonic() + FAST_SCAN_DURATION

    @property
    def update_interval(self) -> timedelta | None:
        """Return the current update interval (fast after commands)."""
        if time.monotonic() < self._fast_poll_until:
            return timedelta(seconds=FAST_SCAN_INTERVAL)
        return timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    @update_interval.setter
    def update_interval(self, value: timedelta | None) -> None:
        """Allow setting the update interval (used by base class)."""
        # We manage the interval dynamically, so just accept the set
        pass

    async def _async_update_data(self) -> dict[str, dict]:
        """Fetch printer data from the API."""
        try:
            printers = await self.api.get_printers()

            # Fetch detailed info for each printer in parallel
            tasks = [self.api.get_printer(p["uuid"]) for p in printers]
            details = await asyncio.gather(*tasks, return_exceptions=True)

            result: dict[str, dict] = {}
            for printer, detail in zip(printers, details):
                uuid = printer["uuid"]
                if isinstance(detail, Exception):
                    _LOGGER.warning(
                        "Failed to fetch detail for printer %s: %s",
                        uuid,
                        detail,
                    )
                    # Fall back to the basic printer data
                    result[uuid] = printer
                else:
                    result[uuid] = detail

            return result

        except ConfigEntryAuthFailed:
            raise
        except ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err


class PrusaConnectJobCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Coordinator for polling job data.

    Data shape: dict keyed by printer UUID, each value is the most recent
    active/current job dict for that printer.
    """

    def __init__(self, hass: HomeAssistant, api: PrusaConnectAPI) -> None:
        """Initialize the job coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_jobs",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, dict]:
        """Fetch job data from the API."""
        try:
            jobs = await self.api.get_jobs()

            # Group jobs by printer UUID, keeping the most recent per printer
            result: dict[str, dict] = {}
            for job in jobs:
                printer_uuid = job.get("printerUuid") or job.get("printer", {}).get(
                    "uuid"
                )
                if not printer_uuid:
                    continue

                existing = result.get(printer_uuid)
                if existing is None or _job_is_more_recent(job, existing):
                    result[printer_uuid] = job

            return result

        except ConfigEntryAuthFailed:
            raise
        except ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err


def _job_is_more_recent(job_a: dict, job_b: dict) -> bool:
    """Check if job_a is more recent than job_b."""
    # Prefer active jobs (PRINTING, PAUSED) over completed ones
    active_states = {"PRINTING", "PAUSED"}
    a_active = job_a.get("state") in active_states
    b_active = job_b.get("state") in active_states
    if a_active and not b_active:
        return True
    if b_active and not a_active:
        return False

    # Otherwise compare by ID (higher = more recent)
    return (job_a.get("id") or 0) > (job_b.get("id") or 0)
