"""Timelapse recording: session lifecycle, deduplication and cleanup.

The failure modes worth guarding are all about the filesystem. Frames are the
only copy of a print that has already happened, so losing them to a failed
encode is unrecoverable; equally, a session that never ends fills the disk.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from custom_components.prusa_connect.const import (
    TIMELAPSE_MAX_FRAMES,
    PrinterState,
)
from custom_components.prusa_connect.timelapse import (
    TimelapseError,
    TimelapseRecorder,
    safe_name,
)

PRINTER = "fbb7c3aa-09c7-4963-9bd3-836038dbc222"
CAMERA_ID = 588016


class _Api:
    """Serves a scripted sequence of snapshots."""

    def __init__(self, images: list[bytes | None] | None = None) -> None:
        self.images = images if images is not None else [b"a", b"b", b"c"]
        self.calls = 0
        self.error: Exception | None = None

    async def get_camera_snapshot(self, camera_id):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.error is not None:
            raise self.error
        index = min(self.calls - 1, len(self.images) - 1)
        return self.images[index]


class _JobCoordinator:
    """Stand-in for the job coordinator.

    Separate on purpose: the printer payload carries no job at all, which is
    how the finished videos ended up named after the printer.
    """

    def __init__(self) -> None:
        self.data: dict = {}

    def set_job(self, job: dict | None, state: str = "PRINTING") -> None:
        if job is None:
            self.data.pop(PRINTER, None)
        else:
            self.data[PRINTER] = {**job, "state": state}


class _Coordinator:
    """Stand-in for the printer coordinator."""

    def __init__(
        self, state: str = PrinterState.IDLE, jobs: _JobCoordinator | None = None
    ) -> None:
        self.data = {PRINTER: {"printer_state": state, "name": "Core One"}}
        self.jobs = jobs
        self.listeners: list = []

    def async_add_listener(self, listener):  # noqa: ANN001, ANN201
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def set_state(
        self, state: str, job: dict | None = None, job_state: str = "PRINTING"
    ) -> None:
        self.data[PRINTER]["printer_state"] = state
        if job is not None and self.jobs is not None:
            self.jobs.set_job(job, job_state)
        for listener in list(self.listeners):
            listener()


class _Config:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.media_dirs = {"local": str(root / "media")}

    def path(self, *parts: str) -> str:
        return str(self.root.joinpath(*parts))


class _Hass:
    """Enough HomeAssistant to run the recorder's I/O and tasks."""

    def __init__(self, root: Path) -> None:
        self.config = _Config(root)
        self.tasks: list = []

    async def async_add_executor_job(self, func, *args):  # noqa: ANN001, ANN201
        return func(*args)

    def async_create_task(self, coro):  # noqa: ANN001, ANN201
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        while self.tasks:
            pending, self.tasks = self.tasks, []
            await asyncio.gather(*pending)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A recorder wired to fakes, with encoding stubbed out."""
    encoded: list[tuple] = []

    async def fake_encode(self, session, output) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        encoded.append((session, output))

    monkeypatch.setattr(TimelapseRecorder, "_async_encode", fake_encode)

    def build(state=PrinterState.IDLE, api=None, cameras=None, job=None):
        hass = _Hass(tmp_path)
        jobs = _JobCoordinator()
        if job is not None:
            jobs.set_job(job)
        coordinator = _Coordinator(state, jobs)
        recorder = TimelapseRecorder(
            hass,
            api or _Api(),
            coordinator,
            {PRINTER: CAMERA_ID} if cameras is None else cameras,
            jobs,
        )
        recorder.async_start()
        build.jobs = jobs
        return recorder, hass, coordinator

    build.encoded = encoded
    return build


class TestFilenames:
    """Job names reach the filesystem, so they have to be tamed."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Benchy.gcode", "Benchy.gcode"),
            ("my print v2.bgcode", "my_print_v2.bgcode"),
            ("../../etc/passwd", "etc_passwd"),
            ("kryt–vršek.gcode", "kryt_vr_ek.gcode"),
            ("", "print"),
            ("///", "print"),
        ],
    )
    def test_names_are_made_safe(self, raw: str, expected: str) -> None:
        assert safe_name(raw) == expected

    def test_long_names_are_truncated(self) -> None:
        assert len(safe_name("x" * 500)) == 80

    def test_a_traversal_cannot_escape_the_directory(self) -> None:
        """A job named for a parent path must not write outside the target."""
        assert "/" not in safe_name("../../../etc/shadow")


class TestSessionLifecycle:
    """Sessions start with the print and end with it."""

    @pytest.mark.asyncio
    async def test_printing_starts_a_session(self, setup) -> None:
        recorder, _, coordinator = setup()
        assert recorder._sessions == {}

        coordinator.set_state(PrinterState.PRINTING)
        assert PRINTER in recorder._sessions

    @pytest.mark.asyncio
    async def test_finishing_encodes_and_clears(self, setup) -> None:
        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(3):
            await recorder._async_capture_all()

        coordinator.set_state(PrinterState.FINISHED)
        await hass.drain()

        assert recorder._sessions == {}
        assert len(setup.encoded) == 1

    @pytest.mark.asyncio
    async def test_pausing_keeps_recording(self, setup) -> None:
        """A paused print is still in progress; the session must survive it."""
        recorder, _, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        coordinator.set_state(PrinterState.PAUSED)
        assert PRINTER in recorder._sessions

    @pytest.mark.asyncio
    async def test_a_failed_print_is_still_worth_keeping(self, setup) -> None:
        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(3):
            await recorder._async_capture_all()

        coordinator.set_state(PrinterState.ERROR)
        await hass.drain()
        assert len(setup.encoded) == 1

    @pytest.mark.asyncio
    async def test_printer_without_a_camera_is_skipped(self, setup) -> None:
        recorder, _, coordinator = setup(cameras={})
        coordinator.set_state(PrinterState.PRINTING)
        assert recorder._sessions == {}

    @pytest.mark.asyncio
    async def test_sampling_stops_when_nothing_is_recording(self, setup) -> None:
        """A timer left running would poll the API forever for nothing."""
        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        assert recorder._cancel_timer is not None

        coordinator.set_state(PrinterState.FINISHED)
        await hass.drain()
        assert recorder._cancel_timer is None


class TestCapture:
    """What ends up on disk."""

    @pytest.mark.asyncio
    async def test_frames_are_written_in_order(self, setup) -> None:
        recorder, _, coordinator = setup(api=_Api([b"a", b"b", b"c"]))
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(3):
            await recorder._async_capture_all()

        session = recorder._sessions[PRINTER]
        names = sorted(p.name for p in session.frame_dir.iterdir())
        assert names == ["frame_000001.jpg", "frame_000002.jpg", "frame_000003.jpg"]

    @pytest.mark.asyncio
    async def test_identical_snapshots_are_not_stored_twice(self, setup) -> None:
        """The camera refreshes on its own schedule; polls repeat frames.

        Storing them would pad the video with motionless frames and waste disk
        on a long print.
        """
        recorder, _, coordinator = setup(api=_Api([b"same", b"same", b"same"]))
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(3):
            await recorder._async_capture_all()

        assert recorder._sessions[PRINTER].frames == 1

    @pytest.mark.asyncio
    async def test_numbering_stays_contiguous_across_skips(self, setup) -> None:
        """ffmpeg reads `frame_%06d.jpg` and stops dead at the first gap.

        Duplicates, empty responses and fetch errors all skip a tick, so if the
        counter advanced on a skip the video would silently truncate at the
        first hole instead of failing loudly.
        """
        recorder, _, coordinator = setup(
            api=_Api([b"a", b"a", None, b"b", b"a", b"c"])
        )
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(6):
            await recorder._async_capture_all()

        session = recorder._sessions[PRINTER]
        numbers = sorted(
            int(p.stem.split("_")[1]) for p in session.frame_dir.iterdir()
        )
        assert numbers == list(range(1, len(numbers) + 1))
        assert session.frames == len(numbers)

    @pytest.mark.asyncio
    async def test_a_failed_fetch_does_not_end_the_recording(self, setup) -> None:
        api = _Api()
        api.error = RuntimeError("connection reset")
        recorder, _, coordinator = setup(api=api)
        coordinator.set_state(PrinterState.PRINTING)
        await recorder._async_capture_all()

        assert PRINTER in recorder._sessions
        assert recorder._sessions[PRINTER].frames == 0

    @pytest.mark.asyncio
    async def test_an_empty_snapshot_is_ignored(self, setup) -> None:
        recorder, _, coordinator = setup(api=_Api([None]))
        coordinator.set_state(PrinterState.PRINTING)
        await recorder._async_capture_all()
        assert recorder._sessions[PRINTER].frames == 0

    @pytest.mark.asyncio
    async def test_overlapping_ticks_do_not_interleave(self, setup) -> None:
        """A slow fetch must not let the next tick write frames out of order."""
        recorder, _, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)

        recorder._capturing = True
        await recorder._async_capture_all()
        assert recorder._sessions[PRINTER].frames == 0

    @pytest.mark.asyncio
    async def test_capture_stops_at_the_frame_ceiling(self, setup) -> None:
        """A print that never reports finishing must not fill the disk."""
        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        session = recorder._sessions[PRINTER]
        session.frames = TIMELAPSE_MAX_FRAMES

        await recorder._async_capture_all()
        await hass.drain()
        assert recorder._sessions == {}


class TestNaming:
    """The video is named after the print, which lives in the job coordinator."""

    @pytest.mark.asyncio
    async def test_named_after_the_file_being_printed(self, setup) -> None:
        recorder, hass, coordinator = setup()
        coordinator.set_state(
            PrinterState.PRINTING, job={"file": {"display_name": "Benchy.gcode"}}
        )
        assert recorder._sessions[PRINTER].label == "Benchy.gcode"

    @pytest.mark.asyncio
    async def test_a_stale_finished_job_does_not_name_it(self, setup) -> None:
        """Both coordinators poll on the same interval.

        When a printer is first seen printing, the job coordinator can still be
        showing the *previous* job. Naming this recording after that would be
        actively misleading — worse than falling back to the printer.
        """
        recorder, hass, coordinator = setup()
        setup.jobs.set_job({"file": {"display_name": "Previous.gcode"}}, "FINISHED")
        coordinator.set_state(PrinterState.PRINTING)

        assert recorder._sessions[PRINTER].label == "Core One"

    @pytest.mark.asyncio
    async def test_the_name_is_picked_up_once_the_job_appears(self, setup) -> None:
        """The lag is normal, so the name is resolved again on every frame."""
        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        assert recorder._sessions[PRINTER].label == "Core One", "not known yet"

        setup.jobs.set_job({"file": {"display_name": "Benchy.gcode"}})
        await recorder._async_capture_all()

        assert recorder._sessions[PRINTER].label == "Benchy.gcode"

    @pytest.mark.asyncio
    async def test_a_later_job_cannot_rename_the_recording(self, setup) -> None:
        recorder, hass, coordinator = setup()
        coordinator.set_state(
            PrinterState.PRINTING, job={"file": {"display_name": "First.gcode"}}
        )
        setup.jobs.set_job({"file": {"display_name": "Second.gcode"}})
        await recorder._async_capture_all()

        assert recorder._sessions[PRINTER].label == "First.gcode"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_printer_when_there_is_no_job(self, setup) -> None:
        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        await recorder._async_capture_all()

        assert recorder._sessions[PRINTER].label == "Core One"


class TestMissedFrames:
    """A recording that quietly stops producing frames must say so."""

    @pytest.mark.asyncio
    async def test_repeated_failures_are_reported(self, setup, caplog) -> None:
        """Every miss was logged at debug, so a dead recording left no trace."""
        recorder, hass, coordinator = setup(api=_Api([None, None, None, None]))
        coordinator.set_state(PrinterState.PRINTING)

        with caplog.at_level(logging.WARNING):
            for _ in range(4):
                await recorder._async_capture_all()

        assert "missed 3 frames in a row" in caplog.text

    @pytest.mark.asyncio
    async def test_one_bad_frame_stays_quiet(self, setup, caplog) -> None:
        recorder, hass, coordinator = setup(api=_Api([b"a", None, b"b"]))
        coordinator.set_state(PrinterState.PRINTING)

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await recorder._async_capture_all()

        assert "missed" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_run_resets_when_a_frame_arrives(self, setup, caplog) -> None:
        """Only a *consecutive* run means the recording has stopped working."""
        recorder, hass, coordinator = setup(
            api=_Api([None, None, b"a", None, None, b"b"])
        )
        coordinator.set_state(PrinterState.PRINTING)

        with caplog.at_level(logging.WARNING):
            for _ in range(6):
                await recorder._async_capture_all()

        assert "missed" not in caplog.text
        assert recorder._sessions[PRINTER].frames == 2


class TestOutput:
    """Where the video goes, and what happens when it cannot be made."""

    @pytest.mark.asyncio
    async def test_video_lands_in_the_media_library(self, setup) -> None:
        recorder, hass, coordinator = setup()
        coordinator.set_state(
            PrinterState.PRINTING, job={"file": {"display_name": "Benchy.gcode"}}
        )
        for _ in range(3):
            await recorder._async_capture_all()
        coordinator.set_state(PrinterState.FINISHED)
        await hass.drain()

        _, output = setup.encoded[0]
        assert output.parent == Path(hass.config.media_dirs["local"]) / "prusa_connect"
        assert output.name.endswith("_Benchy.gcode.mp4")

    @pytest.mark.asyncio
    async def test_frames_are_removed_after_a_successful_encode(self, setup) -> None:
        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(3):
            await recorder._async_capture_all()
        frame_dir = recorder._sessions[PRINTER].frame_dir

        coordinator.set_state(PrinterState.FINISHED)
        await hass.drain()
        assert not frame_dir.exists()

    @pytest.mark.asyncio
    async def test_frames_are_kept_when_encoding_fails(
        self, setup, monkeypatch
    ) -> None:
        """Frames are the only copy of a print that already happened."""

        async def boom(self, session, output) -> None:
            raise TimelapseError("ffmpeg not found")

        recorder, hass, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(3):
            await recorder._async_capture_all()
        frame_dir = recorder._sessions[PRINTER].frame_dir

        monkeypatch.setattr(TimelapseRecorder, "_async_encode", boom)
        coordinator.set_state(PrinterState.FINISHED)
        await hass.drain()

        assert frame_dir.exists()
        assert len(list(frame_dir.iterdir())) == 3

    @pytest.mark.asyncio
    async def test_a_print_too_short_to_film_is_discarded(self, setup) -> None:
        """One frame is not a video; encoding it would just litter the library."""
        recorder, hass, coordinator = setup(api=_Api([b"only"]))
        coordinator.set_state(PrinterState.PRINTING)
        await recorder._async_capture_all()
        frame_dir = recorder._sessions[PRINTER].frame_dir

        coordinator.set_state(PrinterState.FINISHED)
        await hass.drain()

        assert setup.encoded == []
        assert not frame_dir.exists()


class TestShutdown:
    """Unloading must not strand a recording."""

    @pytest.mark.asyncio
    async def test_stop_finalises_a_session_in_progress(self, setup) -> None:
        recorder, _, coordinator = setup()
        coordinator.set_state(PrinterState.PRINTING)
        for _ in range(3):
            await recorder._async_capture_all()

        await recorder.async_stop()

        assert recorder._sessions == {}
        assert len(setup.encoded) == 1

    @pytest.mark.asyncio
    async def test_stop_detaches_from_the_coordinator(self, setup) -> None:
        recorder, _, coordinator = setup()
        await recorder.async_stop()
        assert coordinator.listeners == []
