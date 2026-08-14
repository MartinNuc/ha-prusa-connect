"""Build a timelapse of each print from the camera's snapshots.

The camera can record timelapses itself, but only onto a microSD card, and
nothing in Connect's API hands the resulting file back over the internet — so
that path is unusable for a printer you cannot walk up to. Snapshots go the
other way: Connect already holds the latest one, they are 1920x1080 where the
live stream is 640x480, and fetching them costs the printer's network nothing.

A session lasts one print. Frames accumulate in a working directory outside the
media library, and only the finished video is published to it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import logging
from pathlib import Path
import re
import shutil

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import PrusaConnectAPI
from .const import (
    TIMELAPSE_FPS,
    TIMELAPSE_INTERVAL,
    TIMELAPSE_MAX_FRAMES,
    TIMELAPSE_MEDIA_DIR,
    TIMELAPSE_WORK_DIR,
    PrinterState,
)
from .coordinator import PrusaConnectPrinterCoordinator

_LOGGER = logging.getLogger(__name__)

# Recording runs while the printer is in one of these states. PAUSED is
# included deliberately: the print is still in progress, and a paused printer
# produces identical frames that deduplication drops anyway.
RECORDING_STATES = frozenset({PrinterState.PRINTING, PrinterState.PAUSED})

# Reaching any of these means the print is over and the video can be built.
# ERROR and STOPPED are included because a print that failed halfway is often
# exactly the one worth watching back.
FINISHED_STATES = frozenset(
    {
        PrinterState.FINISHED,
        PrinterState.STOPPED,
        PrinterState.ERROR,
        PrinterState.IDLE,
        PrinterState.READY,
    }
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    """Reduce a job name to something safe to use as a filename.

    Job names come from the sliced file and routinely contain spaces, slashes
    and accented characters. Anything outside a conservative set becomes an
    underscore so the result cannot escape its directory or surprise a shell.
    """
    cleaned = _UNSAFE.sub("_", name).strip("._")
    return cleaned[:80] or "print"


@dataclass
class TimelapseSession:
    """One print being recorded."""

    printer_uuid: str
    camera_id: int
    job_name: str
    started: datetime
    frame_dir: Path
    frames: int = 0
    digests: set[str] = field(default_factory=set)

    @property
    def video_name(self) -> str:
        """Filename for the finished video, unique per print."""
        stamp = self.started.strftime("%Y%m%d-%H%M%S")
        return f"{stamp}_{safe_name(self.job_name)}.mp4"


class TimelapseRecorder:
    """Records a timelapse per printer, driven by printer state."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PrusaConnectAPI,
        coordinator: PrusaConnectPrinterCoordinator,
        cameras: dict[str, int],
    ) -> None:
        """Initialize the recorder.

        ``cameras`` maps printer uuid to the camera id to photograph it with.
        """
        self.hass = hass
        self._api = api
        self._coordinator = coordinator
        self._cameras = cameras
        self._sessions: dict[str, TimelapseSession] = {}
        self._cancel_timer: callback | None = None
        self._cancel_listener: callback | None = None
        self._capturing = False

    @callback
    def async_start(self) -> None:
        """Begin following printer state."""
        self._cancel_listener = self._coordinator.async_add_listener(
            self._handle_coordinator_update
        )
        self._handle_coordinator_update()

    async def async_stop(self) -> None:
        """Stop recording, finishing any video in progress.

        Called on unload and shutdown. Sessions are finalised rather than
        discarded so a reload mid-print still yields the frames captured so far.
        """
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
        self._async_stop_timer()

        for printer_uuid in list(self._sessions):
            await self._async_finish(printer_uuid)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Start and stop sessions as printers change state."""
        for printer_uuid, printer in (self._coordinator.data or {}).items():
            state = printer.get("printer_state") or printer.get("state")
            recording = printer_uuid in self._sessions

            if state in RECORDING_STATES and not recording:
                self._async_begin(printer_uuid, printer)
            elif recording and state in FINISHED_STATES:
                self.hass.async_create_task(self._async_finish(printer_uuid))

    @callback
    def _async_begin(self, printer_uuid: str, printer: dict) -> None:
        """Open a session for a printer that has started printing."""
        camera_id = self._cameras.get(printer_uuid)
        if camera_id is None:
            return

        started = dt_util.now()
        job_name = _job_name(printer)
        frame_dir = (
            Path(self.hass.config.path(TIMELAPSE_WORK_DIR))
            / printer_uuid
            / started.strftime("%Y%m%d-%H%M%S")
        )

        self._sessions[printer_uuid] = TimelapseSession(
            printer_uuid=printer_uuid,
            camera_id=camera_id,
            job_name=job_name,
            started=started,
            frame_dir=frame_dir,
        )
        _LOGGER.info("Recording a timelapse of %s", job_name)
        self._async_start_timer()

    @callback
    def _async_start_timer(self) -> None:
        """Sample on an interval while at least one session is open."""
        if self._cancel_timer is None:
            self._cancel_timer = async_track_time_interval(
                self.hass,
                self._async_capture_all,
                timedelta(seconds=TIMELAPSE_INTERVAL),
            )

    @callback
    def _async_stop_timer(self) -> None:
        """Stop sampling once nothing is being recorded."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    async def _async_capture_all(self, _now: datetime | None = None) -> None:
        """Capture one frame for every open session.

        Guarded against overlap: a slow fetch must not let the next tick start a
        second round and write frames out of order.
        """
        if self._capturing:
            return
        self._capturing = True
        try:
            for printer_uuid in list(self._sessions):
                await self._async_capture(printer_uuid)
        finally:
            self._capturing = False

    async def _async_capture(self, printer_uuid: str) -> None:
        """Fetch one snapshot and keep it if it is new."""
        session = self._sessions.get(printer_uuid)
        if session is None:
            return

        if session.frames >= TIMELAPSE_MAX_FRAMES:
            _LOGGER.warning(
                "Timelapse for %s reached %d frames; stopping capture",
                session.job_name,
                TIMELAPSE_MAX_FRAMES,
            )
            await self._async_finish(printer_uuid)
            return

        try:
            image = await self._api.get_camera_snapshot(session.camera_id)
        except Exception as err:  # noqa: BLE001 - one bad frame must not end the print's recording
            _LOGGER.debug("Timelapse frame fetch failed: %s", err)
            return

        if not image:
            return

        # The camera refreshes on its own schedule, so consecutive polls often
        # return the identical JPEG. Storing those would stretch the video with
        # motionless frames and waste disk.
        digest = hashlib.sha256(image).hexdigest()
        if digest in session.digests:
            return
        session.digests.add(digest)

        session.frames += 1
        path = session.frame_dir / f"frame_{session.frames:06d}.jpg"
        try:
            await self.hass.async_add_executor_job(_write_frame, path, image)
        except OSError as err:
            session.frames -= 1
            session.digests.discard(digest)
            _LOGGER.error("Could not write timelapse frame: %s", err)

    async def _async_finish(self, printer_uuid: str) -> None:
        """Encode a finished session and clean up after it."""
        session = self._sessions.pop(printer_uuid, None)
        if not self._sessions:
            self._async_stop_timer()
        if session is None:
            return

        if session.frames < 2:
            _LOGGER.debug(
                "Discarding timelapse of %s: only %d frame(s)",
                session.job_name,
                session.frames,
            )
            await self.hass.async_add_executor_job(_remove_dir, session.frame_dir)
            return

        media_dir = Path(_media_root(self.hass)) / TIMELAPSE_MEDIA_DIR
        output = media_dir / session.video_name

        try:
            await self.hass.async_add_executor_job(_ensure_dir, media_dir)
            await self._async_encode(session, output)
        except TimelapseError as err:
            # Keep the frames: they are the only copy, and the user can encode
            # them by hand or retry once the cause is fixed.
            _LOGGER.error(
                "Could not build the timelapse for %s (%s). Frames kept in %s",
                session.job_name,
                err,
                session.frame_dir,
            )
            return

        _LOGGER.info(
            "Timelapse of %s written to %s (%d frames)",
            session.job_name,
            output,
            session.frames,
        )
        await self.hass.async_add_executor_job(_remove_dir, session.frame_dir)

    async def _async_encode(self, session: TimelapseSession, output: Path) -> None:
        """Run ffmpeg over the captured frames."""
        binary = _ffmpeg_binary(self.hass)

        args = [
            binary,
            "-nostdin",
            "-y",
            "-framerate", str(TIMELAPSE_FPS),
            "-i", str(session.frame_dir / "frame_%06d.jpg"),
            "-c:v", "libx264",
            "-preset", "veryfast",
            # Odd dimensions are not representable in yuv420p, which is what
            # makes the result playable everywhere.
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except OSError as err:
            raise TimelapseError(f"could not run ffmpeg: {err}") from err

        if process.returncode != 0:
            tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
            raise TimelapseError(" / ".join(tail) or f"exit {process.returncode}")


class TimelapseError(Exception):
    """Encoding a timelapse failed."""


def _job_name(printer: dict) -> str:
    """Name of the job the printer is running, for labelling the video."""
    job = printer.get("job") or {}
    file = job.get("file") or {}
    return (
        file.get("display_name")
        or file.get("name")
        or job.get("path")
        or printer.get("name")
        or "print"
    )


def _media_root(hass: HomeAssistant) -> str:
    """Where finished videos are published.

    Prefers the configured local media directory so the result shows up in the
    media browser; falls back to the conventional path when none is set.
    """
    media_dirs = getattr(hass.config, "media_dirs", None) or {}
    return media_dirs.get("local") or hass.config.path("media")


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    """The ffmpeg binary Home Assistant is configured to use."""
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except (ImportError, KeyError, AttributeError):
        return "ffmpeg"


def _write_frame(path: Path, image: bytes) -> None:
    """Write one frame, creating its directory on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)


def _ensure_dir(path: Path) -> None:
    """Create a directory if it is not already there."""
    path.mkdir(parents=True, exist_ok=True)


def _remove_dir(path: Path) -> None:
    """Delete a directory tree, ignoring one that is already gone."""
    shutil.rmtree(path, ignore_errors=True)
