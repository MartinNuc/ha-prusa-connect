"""Camera entity behaviour: capability gating and WebRTC session lifecycle.

The lifecycle matters more than it looks. A session holds a WebRTC connection
open through Prusa's TURN relay, so one that outlives its viewer keeps
consuming somebody else's bandwidth. Every exit path — success, failure, the
viewer leaving, the entity being removed — has to close it.
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.components.camera import CameraEntityFeature, WebRTCAnswer
from homeassistant.exceptions import HomeAssistantError

from custom_components.prusa_connect.camera import PrusaConnectCamera
from custom_components.prusa_connect.signaling import SignalingError

CAMERA = {
    "id": 588016,
    "name": "Buddy3D Camera",
    "token": "CAMERATOKEN000000000",
    "features": ["GetSnapshot", "VideoStream", "WebRtc"],
}
CAMERA_NO_WEBRTC = {
    "id": 588017,
    "name": "Old Camera",
    "token": "CAMERATOKEN000000001",
    "features": ["GetSnapshot"],
}
PRINTER_UUID = "fbb7c3aa-09c7-4963-9bd3-836038dbc222"


class _Api:
    """Minimal stand-in for the API client."""

    access_token = "ACCESS"

    def __init__(self, snapshot: bytes | None = b"jpeg") -> None:
        self._snapshot = snapshot
        self.environment_calls = 0

    async def get_camera_snapshot(self, camera_id):  # noqa: ANN001, ANN201
        self.last_snapshot_id = camera_id
        return self._snapshot

    async def get_environment(self):  # noqa: ANN201
        self.environment_calls += 1
        return {
            "CAMERA_SIGNALING_SERVER": "signal.example.com",
            "CAMERA_WEBRTC_CONFIG_URL": "https://cfg.example.com/v1/x",
        }


class _Session:
    """Records what the entity does with a stream session."""

    instances: list["_Session"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.started_with: str | None = None
        self.closed = False
        self.candidates: list[tuple[str, str | None]] = []
        self.error: Exception | None = None
        _Session.instances.append(self)

    async def start(self, offer_sdp: str) -> str:
        self.started_with = offer_sdp
        if self.error is not None:
            raise self.error
        return "v=0\r\nANSWER\r\n"

    async def add_viewer_candidate(self, candidate: str, mid) -> None:  # noqa: ANN001
        self.candidates.append((candidate, mid))

    async def close(self) -> None:
        self.closed = True


class _Hass:
    """Just enough HomeAssistant to schedule the teardown task."""

    def __init__(self) -> None:
        self.tasks: list = []

    def async_create_task(self, coro):  # noqa: ANN001, ANN201
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


@pytest.fixture(autouse=True)
def _fresh_sessions(monkeypatch: pytest.MonkeyPatch):
    """Substitute the stream session and reset the recorder."""
    _Session.instances = []
    monkeypatch.setattr(
        "custom_components.prusa_connect.camera.CameraStreamSession", _Session
    )
    return _Session


def make_camera(camera: dict = CAMERA, api: _Api | None = None) -> PrusaConnectCamera:
    """Build an entity with its Home Assistant wiring stubbed out."""
    entity = PrusaConnectCamera(object(), api or _Api(), PRINTER_UUID, camera)
    entity.hass = _Hass()
    entity.async_write_ha_state = lambda: entity.state_writes.append(entity.is_streaming)
    entity.state_writes = []
    return entity


class TestCapabilities:
    """What the entity claims it can do."""

    def test_streaming_camera_advertises_stream_feature(self) -> None:
        entity = make_camera()
        assert entity.supported_features == CameraEntityFeature.STREAM

    def test_idle_until_someone_watches(self) -> None:
        """`supported_features` says it can stream; `is_streaming` says it is.

        Reporting "streaming" while nobody is watching makes the state useless
        for automations and misrepresents what the camera is doing.
        """
        assert make_camera().is_streaming is False

    def test_camera_without_webrtc_does_not_advertise_streaming(self) -> None:
        """Only some cameras can stream; the rest stay snapshot-only."""
        entity = make_camera(CAMERA_NO_WEBRTC)
        assert not getattr(entity, "_attr_supported_features", 0)
        assert entity.is_streaming is False

    def test_camera_without_a_token_cannot_stream(self) -> None:
        """The token authenticates signalling; without it streaming is impossible."""
        entity = make_camera({**CAMERA, "token": None})
        assert entity.is_streaming is False

    def test_unique_id_is_stable(self) -> None:
        assert make_camera().unique_id == f"{PRINTER_UUID}_camera_588016"


class TestSnapshots:
    """The snapshot path, which every camera supports."""

    @pytest.mark.asyncio
    async def test_snapshot_comes_from_the_api(self) -> None:
        api = _Api(b"image-bytes")
        entity = make_camera(api=api)
        assert await entity.async_camera_image() == b"image-bytes"
        assert api.last_snapshot_id == 588016


class TestWebRTCOffer:
    """Answering a viewer."""

    @pytest.mark.asyncio
    async def test_answers_the_viewer(self) -> None:
        entity = make_camera()
        sent = []
        await entity.async_handle_async_webrtc_offer("v=0\r\nOFFER\r\n", "s1", sent.append)

        assert sent == [WebRTCAnswer("v=0\r\nANSWER\r\n")]
        assert _Session.instances[0].started_with == "v=0\r\nOFFER\r\n"

    @pytest.mark.asyncio
    async def test_rejects_cameras_that_cannot_stream(self) -> None:
        entity = make_camera(CAMERA_NO_WEBRTC)
        with pytest.raises(HomeAssistantError, match="does not support"):
            await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)
        assert not _Session.instances

    @pytest.mark.asyncio
    async def test_environment_is_read_once_per_entity(self) -> None:
        """The runtime config is stable; refetching it per viewer is waste."""
        api = _Api()
        entity = make_camera(api=api)
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s2", lambda _m: None)
        assert api.environment_calls == 1

    @pytest.mark.asyncio
    async def test_failed_session_is_closed_and_forgotten(self) -> None:
        """A session that never started must not linger holding a TURN allocation."""
        entity = make_camera()

        original_init = _Session.__init__

        def failing_init(self, *args, **kwargs) -> None:
            original_init(self, *args, **kwargs)
            self.error = SignalingError("camera offline")

        _Session.__init__ = failing_init
        try:
            with pytest.raises(HomeAssistantError, match="Could not start"):
                await entity.async_handle_async_webrtc_offer(
                    "v=0\r\n", "s1", lambda _m: None
                )
        finally:
            _Session.__init__ = original_init

        assert _Session.instances[0].closed is True
        assert entity._sessions == {}

    @pytest.mark.asyncio
    async def test_unexpected_errors_are_also_cleaned_up(self) -> None:
        entity = make_camera()

        original_init = _Session.__init__

        def failing_init(self, *args, **kwargs) -> None:
            original_init(self, *args, **kwargs)
            self.error = RuntimeError("boom")

        _Session.__init__ = failing_init
        try:
            with pytest.raises(HomeAssistantError):
                await entity.async_handle_async_webrtc_offer(
                    "v=0\r\n", "s1", lambda _m: None
                )
        finally:
            _Session.__init__ = original_init

        assert _Session.instances[0].closed is True


class TestSessionLifecycle:
    """Sessions must not outlive their viewer."""

    @pytest.mark.asyncio
    async def test_candidates_reach_the_right_session(self) -> None:
        entity = make_camera()
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s2", lambda _m: None)

        candidate = type("C", (), {"candidate": "candidate:1 1 UDP", "sdp_mid": "0"})()
        await entity.async_on_webrtc_candidate("s2", candidate)

        assert _Session.instances[0].candidates == []
        assert _Session.instances[1].candidates == [("candidate:1 1 UDP", "0")]

    @pytest.mark.asyncio
    async def test_candidate_for_unknown_session_is_ignored(self) -> None:
        """Candidates can arrive after teardown; that must not raise."""
        entity = make_camera()
        candidate = type("C", (), {"candidate": "candidate:1", "sdp_mid": "0"})()
        await entity.async_on_webrtc_candidate("never-existed", candidate)

    @pytest.mark.asyncio
    async def test_closing_a_session_releases_it(self) -> None:
        entity = make_camera()
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)

        entity.close_webrtc_session("s1")
        await asyncio.gather(*entity.hass.tasks)

        assert _Session.instances[0].closed is True
        assert entity._sessions == {}

    @pytest.mark.asyncio
    async def test_closing_an_unknown_session_is_harmless(self) -> None:
        make_camera().close_webrtc_session("never-existed")

    @pytest.mark.asyncio
    async def test_removal_closes_every_open_session(self) -> None:
        """Reloading the integration must not strand live connections."""
        entity = make_camera()
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s2", lambda _m: None)

        await entity.async_will_remove_from_hass()

        assert all(session.closed for session in _Session.instances)
        assert entity._sessions == {}


class TestStreamingState:
    """The entity state must track real viewers, not mere capability."""

    @pytest.mark.asyncio
    async def test_becomes_streaming_only_once_a_viewer_connects(self) -> None:
        entity = make_camera()
        assert entity.is_streaming is False

        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)
        assert entity.is_streaming is True
        assert entity.state_writes == [True]

    @pytest.mark.asyncio
    async def test_returns_to_idle_when_the_last_viewer_leaves(self) -> None:
        entity = make_camera()
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s2", lambda _m: None)

        entity.close_webrtc_session("s1")
        assert entity.is_streaming is True, "one viewer remains"

        entity.close_webrtc_session("s2")
        assert entity.is_streaming is False
        await asyncio.gather(*entity.hass.tasks)

    @pytest.mark.asyncio
    async def test_state_is_written_only_when_it_changes(self) -> None:
        """A write per viewer would churn the state machine for no reason."""
        entity = make_camera()
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s1", lambda _m: None)
        await entity.async_handle_async_webrtc_offer("v=0\r\n", "s2", lambda _m: None)
        assert entity.state_writes == [True]

    @pytest.mark.asyncio
    async def test_failed_session_leaves_the_camera_idle(self) -> None:
        entity = make_camera()
        original_init = _Session.__init__

        def failing_init(self, *args, **kwargs) -> None:
            original_init(self, *args, **kwargs)
            self.error = SignalingError("camera offline")

        _Session.__init__ = failing_init
        try:
            with pytest.raises(HomeAssistantError):
                await entity.async_handle_async_webrtc_offer(
                    "v=0\r\n", "s1", lambda _m: None
                )
        finally:
            _Session.__init__ = original_init

        assert entity.is_streaming is False
        assert entity.state_writes == []
