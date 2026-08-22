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
import time
from pathlib import Path
import re
import shutil

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import PrusaConnectAPI
from .const import (
    TIMELAPSE_FPS,
    TIMELAPSE_TAIL,
    TIMELAPSE_INTERVAL,
    TIMELAPSE_MAX_FRAMES,
    TIMELAPSE_MEDIA_DIR,
    TIMELAPSE_WORK_DIR,
    PrinterState,
)
from .coordinator import (
    ACTIVE_JOB_STATES,
    PrusaConnectJobCoordinator,
    PrusaConnectPrinterCoordinator,
)

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

# Consecutive failed captures before saying so out loud. Three misses is 90
# seconds of a print going unrecorded — past the point of bad luck.
CAPTURE_MISS_LIMIT = 3

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
    printer_name: str
    started: datetime
    frame_dir: Path
    job_name: str | None = None
    frames: int = 0
    misses: int = 0
    # When the print ended and the tail began, as a monotonic deadline. None
    # while the print is still running.
    finish_after: float | None = None
    digests: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        """What to call this recording: the file printed, else the printer."""
        return self.job_name or self.printer_name or "print"

    @property
    def video_name(self) -> str:
        """Filename for the finished video, unique per print."""
        stamp = self.started.strftime("%Y%m%d-%H%M%S")
        return f"{stamp}_{safe_name(self.label)}.mp4"


class TimelapseRecorder:
    """Records a timelapse per printer, driven by printer state."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PrusaConnectAPI,
        coordinator: PrusaConnectPrinterCoordinator,
        cameras: dict[str, int],
        job_coordinator: PrusaConnectJobCoordinator | None = None,
    ) -> None:
        """Initialize the recorder.

        ``cameras`` maps printer uuid to the camera id to photograph it with.
        ``job_coordinator`` supplies the name of the file being printed; the
        printer payload does not carry it.
        """
        self.hass = hass
        self._api = api
        self._coordinator = coordinator
        self._job_coordinator = job_coordinator
        self._cameras = cameras
        self._sessions: dict[str, TimelapseSession] = {}
        self._cancel_timer: callback | None = None
        self._cancel_listener: callback | None = None
        self._capturing = False

    @callback
    def async_start(self) -> None:
        """Begin following printer state."""
        # The media directory is resolved from Home Assistant's own config and
        # differs between installation types, so say where videos will actually
        # land rather than leaving the user to guess.
        _LOGGER.info(
            "Timelapse recording enabled for %d printer(s); videos will be "
            "written to %s",
            len(self._cameras),
            Path(_media_root(self.hass)) / TIMELAPSE_MEDIA_DIR,
        )
        self._cancel_listener = self._coordinator.async_add_listener(
            self._handle_coordinator_update
        )
        self._handle_coordinator_update()
        self.hass.async_create_task(self._async_recover_orphans())

    async def _async_recover_orphans(self) -> None:
        """Encode frames left behind by a session that never finished.

        A restart or a crash mid-print leaves its frames on disk with no video:
        the session lives in memory only, so nothing knows to finish it. Nine
        hours of one print were lost that way before this existed.

        Directories belonging to sessions running now are left alone; every
        other one is encoded on its own, which is the honest outcome — a print
        interrupted by a restart genuinely is two recordings, and half a video
        beats none.
        """
        active = {session.frame_dir for session in self._sessions.values()}
        root = Path(self.hass.config.path(TIMELAPSE_WORK_DIR))

        try:
            orphans = await self.hass.async_add_executor_job(_find_sessions, root)
        except OSError as err:
            _LOGGER.debug("Could not scan for unfinished timelapses: %s", err)
            return

        for frame_dir in orphans:
            if frame_dir in active:
                continue
            frames = await self.hass.async_add_executor_job(_count_frames, frame_dir)
            if frames < 2:
                _LOGGER.debug("Discarding %s: %d frame(s)", frame_dir.name, frames)
                await self.hass.async_add_executor_job(_remove_dir, frame_dir)
                continue

            _LOGGER.info(
                "Found %d frames from an unfinished timelapse (%s); encoding them",
                frames,
                frame_dir.name,
            )
            session = TimelapseSession(
                printer_uuid=frame_dir.parent.name,
                camera_id=0,
                printer_name="",
                started=_started_from_name(frame_dir.name),
                frame_dir=frame_dir,
                job_name="interrupted",
                frames=frames,
            )
            self._sessions[frame_dir.parent.name + ":recovered"] = session
            try:
                await self._async_finish(frame_dir.parent.name + ":recovered")
            except Exception:  # noqa: BLE001 - one bad recovery is not the rest
                _LOGGER.exception("Could not recover %s", frame_dir.name)

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
            elif recording and state in RECORDING_STATES:
                # Printing again: either the print never really stopped or a
                # state blip ended the tail early. Either way, carry on.
                session = self._sessions[printer_uuid]
                if session.finish_after is not None:
                    _LOGGER.debug("Printing resumed; cancelling the tail")
                    session.finish_after = None
            elif recording and state in FINISHED_STATES:
                session = self._sessions[printer_uuid]
                if session.finish_after is None:
                    session.finish_after = time.monotonic() + TIMELAPSE_TAIL
                    _LOGGER.info(
                        "%s finished; recording for another %d seconds",
                        session.label,
                        TIMELAPSE_TAIL,
                    )

    @callback
    def _async_begin(self, printer_uuid: str, printer: dict) -> None:
        """Open a session for a printer that has started printing."""
        camera_id = self._cameras.get(printer_uuid)
        if camera_id is None:
            return

        started = dt_util.now()
        frame_dir = (
            Path(self.hass.config.path(TIMELAPSE_WORK_DIR))
            / printer_uuid
            / started.strftime("%Y%m%d-%H%M%S")
        )

        session = TimelapseSession(
            printer_uuid=printer_uuid,
            camera_id=camera_id,
            printer_name=printer.get("name") or "",
            started=started,
            frame_dir=frame_dir,
        )
        # May well be None here — the job coordinator often has not caught up
        # with the printer yet. It is resolved again on every frame until it
        # answers, and only the finished video's name depends on it.
        session.job_name = self._async_job_name(printer_uuid)

        self._sessions[printer_uuid] = session
        _LOGGER.info("Recording a timelapse of %s", session.label)
        self._async_start_timer()

    @callback
    def _async_frame_missed(self, session: TimelapseSession, reason: str) -> None:
        """Note a frame we could not fetch, and complain if they keep failing.

        A single miss is unremarkable. A run of them means the recording is
        quietly producing nothing, which is exactly what happened once before:
        every failure was logged at debug level, so a print that captured one
        frame and then stopped left no trace of why.
        """
        session.misses += 1
        if session.misses == CAPTURE_MISS_LIMIT:
            _LOGGER.warning(
                "Timelapse of %s has missed %d frames in a row (%s). Recording "
                "continues, but the video will have a gap",
                session.label,
                session.misses,
                reason,
            )
        else:
            _LOGGER.debug("Timelapse frame fetch failed: %s", reason)

    @callback
    def _async_job_name(self, printer_uuid: str) -> str | None:
        """The name of the file this printer is currently printing."""
        if self._job_coordinator is None:
            return None
        job = (self._job_coordinator.data or {}).get(printer_uuid)
        return _job_name(job)

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

            # After capturing, not before: the whole point of the tail is that
            # its last frames make it into the video.
            now = time.monotonic()
            for printer_uuid, session in list(self._sessions.items()):
                if session.finish_after is not None and now >= session.finish_after:
                    await self._async_finish(printer_uuid)
        finally:
            self._capturing = False

    async def _async_capture(self, printer_uuid: str) -> None:
        """Fetch one snapshot and keep it if it is new."""
        session = self._sessions.get(printer_uuid)
        if session is None:
            return

        # Keep asking until the job coordinator catches up. First answer wins,
        # so a job queued behind this one cannot rename the recording.
        if session.job_name is None:
            session.job_name = self._async_job_name(printer_uuid)

        if session.frames >= TIMELAPSE_MAX_FRAMES:
            _LOGGER.warning(
                "Timelapse for %s reached %d frames; stopping capture",
                session.label,
                TIMELAPSE_MAX_FRAMES,
            )
            await self._async_finish(printer_uuid)
            return

        try:
            image = await self._api.get_camera_snapshot(session.camera_id)
        except Exception as err:  # noqa: BLE001 - one bad frame must not end the print's recording
            self._async_frame_missed(session, str(err))
            return

        if not image:
            self._async_frame_missed(session, "the camera returned no image")
            return

        session.misses = 0

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
                session.label,
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
                session.label,
                err,
                session.frame_dir,
            )
            return

        _LOGGER.info(
            "Timelapse of %s written to %s (%d frames)",
            session.label,
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


def _job_name(job: dict | None) -> str | None:
    """Name of the file being printed, for labelling the video.

    Only an *active* job counts. Both coordinators poll on the same interval,
    so at the moment a printer is first seen printing the job coordinator can
    still be showing the previous, finished job — and latching that name onto
    this recording would be worse than having no name at all.
    """
    if not job or job.get("state") not in ACTIVE_JOB_STATES:
        return None
    file = job.get("file") or {}
    return file.get("display_name") or file.get("name") or job.get("path")


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


def _find_sessions(root: Path) -> list[Path]:
    """Every session directory under the working root."""
    if not root.is_dir():
        return []
    return sorted(
        child
        for printer_dir in root.iterdir()
        if printer_dir.is_dir()
        for child in printer_dir.iterdir()
        if child.is_dir()
    )


def _count_frames(path: Path) -> int:
    """How many frames a session directory holds."""
    return sum(1 for entry in path.iterdir() if entry.suffix == ".jpg")


def _started_from_name(name: str) -> datetime:
    """Recover a session's start time from its directory name."""
    try:
        return datetime.strptime(name, "%Y%m%d-%H%M%S")
    except ValueError:
        return dt_util.now()


def _remove_dir(path: Path) -> None:
    """Delete a directory tree, ignoring one that is already gone."""
    shutil.rmtree(path, ignore_errors=True)
