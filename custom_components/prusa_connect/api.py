"""API client for the Prusa Connect app API.

Endpoints live under https://connect.prusa3d.com/app and are authenticated with
the Prusa Account access token as a bearer token. Collection responses are
wrapped in a single-key envelope (``{"printers": [...]}``) rather than returned
as bare lists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed

from .auth import AuthenticationError, refresh_access_token
from .const import (
    API_BASE_URL,
    API_PREFIX,
    CMD_CANCEL_READY,
    CMD_DIALOG_ACTION,
    CMD_PAUSE,
    CMD_RESUME,
    CMD_SET_READY,
    CMD_START_PRINT,
    CMD_STOP,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class PrusaConnectAPI:
    """Client for the Prusa Connect app API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        token_update_callback: Any = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_update_callback = token_update_callback
        self._refresh_lock = asyncio.Lock()

    def update_tokens(self, access_token: str, refresh_token: str) -> None:
        """Update stored tokens."""
        self._access_token = access_token
        self._refresh_token = refresh_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        envelope: str | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        """Make an authenticated API request.

        Refreshes the token once on 401 and retries. When ``envelope`` is set,
        the matching key is unwrapped from the response body.
        """
        url = f"{API_BASE_URL}{API_PREFIX}{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        async with self._session.request(
            method,
            url,
            headers=headers,
            json=json,
            params=params,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 401 and retry_on_401:
                await self._refresh_token_and_persist()
                return await self._request(
                    method,
                    path,
                    json=json,
                    params=params,
                    envelope=envelope,
                    retry_on_401=False,
                )

            if resp.status == 401:
                raise ConfigEntryAuthFailed(
                    "Authentication failed after token refresh"
                )

            if resp.status == 204:
                return None

            resp.raise_for_status()
            result = await resp.json()

            if envelope and isinstance(result, dict):
                return result.get(envelope) or []

            return result

    async def get_bytes(self, path: str) -> bytes | None:
        """Fetch a binary asset (file preview, camera snapshot) with auth.

        ``path`` is a site-relative path that already includes the /app prefix,
        such as the ``preview_url`` returned on a job's file.
        """
        url = path if path.startswith("http") else f"{API_BASE_URL}{path}"

        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._access_token}"}
            async with self._session.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status == 401 and attempt == 1:
                    await self._refresh_token_and_persist()
                    continue
                if resp.status != 200:
                    return None
                return await resp.read()

        return None

    async def _refresh_token_and_persist(self) -> None:
        """Refresh the access token and persist the new tokens."""
        async with self._refresh_lock:
            try:
                tokens = await refresh_access_token(
                    self._session, self._refresh_token
                )
                self._access_token = tokens["access_token"]
                self._refresh_token = tokens["refresh_token"]

                if self._token_update_callback:
                    await self._token_update_callback(tokens)
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    "Token refresh failed — re-authentication required"
                ) from err

    # --- Printers ---

    async def get_printers(self) -> list[dict]:
        """Get all printers visible to the account."""
        return await self._request("GET", "/printers", envelope="printers")

    async def get_printer(self, uuid: str) -> dict:
        """Get the full detail document for a printer."""
        return await self._request("GET", f"/printers/{uuid}")

    async def get_telemetry(self, uuid: str) -> dict:
        """Get recent telemetry series for a printer."""
        return await self._request("GET", f"/printers/{uuid}/telemetry")

    async def get_supported_commands(self, uuid: str) -> list[dict]:
        """Get the commands this printer accepts and the states allowing them."""
        return await self._request(
            "GET", f"/printers/{uuid}/supported-commands", envelope="commands"
        )

    # --- Commands ---

    async def send_command(self, uuid: str, command: str, **kwargs: Any) -> Any:
        """Send a command to a printer.

        Connect takes the command name plus a ``kwargs`` object holding the
        arguments declared by supported-commands.
        """
        return await self._request(
            "POST",
            f"/printers/{uuid}/commands",
            json={"command": command, "kwargs": kwargs},
        )

    async def pause_print(self, uuid: str) -> None:
        """Pause the current print."""
        await self.send_command(uuid, CMD_PAUSE)

    async def resume_print(self, uuid: str) -> None:
        """Resume a paused print."""
        await self.send_command(uuid, CMD_RESUME)

    async def stop_print(self, uuid: str) -> None:
        """Stop the current print."""
        await self.send_command(uuid, CMD_STOP)

    async def set_ready(self, uuid: str) -> None:
        """Mark the printer as ready."""
        await self.send_command(uuid, CMD_SET_READY)

    async def set_unready(self, uuid: str) -> None:
        """Cancel the ready state."""
        await self.send_command(uuid, CMD_CANCEL_READY)

    async def start_print(
        self, uuid: str, path: str, tool_mapping: dict | None = None
    ) -> None:
        """Start printing a file already available to the printer."""
        kwargs: dict[str, Any] = {"path": path}
        if tool_mapping is not None:
            kwargs["tool_mapping"] = tool_mapping
        await self.send_command(uuid, CMD_START_PRINT, **kwargs)

    async def respond_to_dialog(
        self, uuid: str, dialog_id: int, button: str
    ) -> None:
        """Respond to a dialog shown on the printer."""
        await self.send_command(
            uuid, CMD_DIALOG_ACTION, dialog_id=dialog_id, button=button
        )

    async def get_pending_commands(self, uuid: str) -> list[dict]:
        """Get commands queued for a printer."""
        return await self._request(
            "GET", f"/printers/{uuid}/commands", envelope="commands"
        )

    # --- Jobs ---

    async def get_jobs(self, **params: Any) -> list[dict]:
        """Get jobs across all printers."""
        return await self._request(
            "GET", "/jobs", params=params or None, envelope="jobs"
        )

    async def get_printer_jobs(self, uuid: str) -> list[dict]:
        """Get jobs for a single printer."""
        return await self._request(
            "GET", f"/printers/{uuid}/jobs", envelope="jobs"
        )

    async def get_queue(self, uuid: str) -> list[dict]:
        """Get the planned job queue for a printer."""
        return await self._request(
            "GET", f"/printers/{uuid}/queue", envelope="planned_jobs"
        )

    # --- Files ---

    async def get_printer_files(self, uuid: str) -> list[dict]:
        """Get files available on the printer."""
        return await self._request(
            "GET", f"/printers/{uuid}/files", envelope="files"
        )

    # --- Cameras ---

    async def get_cameras(self) -> list[dict]:
        """Get all cameras for the account."""
        return await self._request("GET", "/cameras", envelope="cameras")

    async def get_printer_cameras(self, uuid: str) -> list[dict]:
        """Get cameras attached to a printer."""
        return await self._request(
            "GET", f"/printers/{uuid}/cameras", envelope="cameras"
        )

    async def get_camera_snapshot(self, camera_id: int | str) -> bytes | None:
        """Get the most recent snapshot for a camera, as JPEG bytes."""
        return await self.get_bytes(
            f"{API_PREFIX}/cameras/{camera_id}/snapshots/last"
        )

    # --- Notifications / events ---

    async def get_notifications(self, **params: Any) -> list[dict]:
        """Get account notifications."""
        return await self._request(
            "GET", "/notifications", params=params or None, envelope="notifications"
        )

    async def get_events(self, uuid: str) -> list[dict]:
        """Get recent events for a printer."""
        return await self._request(
            "GET", f"/printers/{uuid}/events", envelope="events"
        )
