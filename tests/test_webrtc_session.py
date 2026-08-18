"""Start-up rules for a camera stream session.

The subtle one: aiortc creates a track object as soon as the camera's offer is
applied, seconds before any media can flow. Treating that as success answers the
viewer with a stream that may never carry a frame — the frontend shows a spinner
forever, and because nothing failed, nothing tears the session down. Each dead
session keeps a TURN allocation, and Prusa's relay starts refusing new ones
("486 Allocation Quota Reached") after a handful, which takes the camera out
entirely until the allocations expire. So the session must wait for the
connection, not the track.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.prusa_connect import webrtc_session as ws
from custom_components.prusa_connect.signaling import SignalingError

ICE_CONFIG = {"ice_servers": [], "policy": None, "ttl": 60}

RELAY_SDP = "v=0\r\na=candidate:1 1 udp 1 10.0.0.1 1 typ relay\r\n"

# Shaped like a real aiortc answer: gathering finishes before
# setLocalDescription returns, so every candidate is already in the SDP.
ANSWER_SDP = (
    "v=0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 102\r\n"
    "a=candidate:1 1 udp 2113937151 192.168.1.5 50470 typ host\r\n"
    "a=candidate:2 1 udp 1677729535 94.112.176.46 50470 typ srflx\r\n"
    "a=candidate:3 1 udp 50340095 34.159.146.76 52342 typ relay\r\n"
    "a=ice-ufrag:abcd\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "a=candidate:1 1 udp 2113937151 192.168.1.5 50470 typ host\r\n"
    "a=candidate:2 1 udp 1677729535 94.112.176.46 50470 typ srflx\r\n"
    "a=candidate:3 1 udp 50340095 34.159.146.76 52342 typ relay\r\n"
    "ANSWER\r\n"
)
HOST_ONLY_SDP = "v=0\r\na=candidate:1 1 udp 1 10.0.0.1 1 typ host\r\n"


class _Api:
    async def get_webrtc_config(self, _url):  # noqa: ANN001, ANN201
        return ICE_CONFIG


class _Description:
    def __init__(self, sdp: str) -> None:
        self.sdp = sdp


class _FakePeerConnection:
    """A peer connection whose state the test drives by hand."""

    instances: list["_FakePeerConnection"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.connectionState = "new"
        self.localDescription = _Description(RELAY_SDP)
        self.closed = False
        self.calls: list[str] = []
        self._handlers: dict[str, list] = {}
        _FakePeerConnection.instances.append(self)

    def on(self, event):  # noqa: ANN001, ANN201
        def register(func):  # noqa: ANN001, ANN202
            self._handlers.setdefault(event, []).append(func)
            return func

        return register

    async def fire(self, event: str, *args) -> None:
        for handler in self._handlers.get(event, []):
            result = handler(*args)
            if asyncio.iscoroutine(result):
                await result

    async def enter(self, state: str) -> None:
        self.connectionState = state
        await self.fire("connectionstatechange")

    def addTrack(self, track):  # noqa: ANN001, ANN201, N802
        return track

    async def setRemoteDescription(self, _desc) -> None:  # noqa: N802
        self.calls.append("setRemoteDescription")

    # Default: the camera did get a relay, which is the healthy case.
    remoteDescription = _Description(  # noqa: N815
        "v=0\r\na=candidate:9 1 udp 1 34.159.146.76 52426 typ relay\r\n"
    )

    async def setLocalDescription(self, desc) -> None:  # noqa: ANN001, N802
        # aiortc finishes gathering here, so the stored SDP is what carries our
        # candidates — which is exactly what the relay diagnosis reads back.
        self.calls.append("setLocalDescription")
        self.localDescription = desc

    async def createAnswer(self):  # noqa: ANN201, N802
        return _Description(ANSWER_SDP)

    async def close(self) -> None:
        self.closed = True


class _FakeSignaling:
    """Replays a camera offer on request; records that it was closed."""

    instances: list["_FakeSignaling"] = []

    def __init__(self, *_args, on_offer=None, on_candidate=None, **_kwargs) -> None:  # noqa: ANN001
        self.on_offer = on_offer
        self.on_candidate = on_candidate
        self.closed = False
        self.answers: list[str] = []
        self.candidates: list[tuple[str, str]] = []
        _FakeSignaling.instances.append(self)

    async def connect(self) -> None:
        return None

    async def request_stream(self, *_args) -> None:
        return None

    async def send_answer(self, sdp: str) -> None:
        self.answers.append(sdp)

    async def send_candidate(self, candidate: str, mid: str) -> None:
        self.candidates.append((candidate, mid))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    _FakePeerConnection.instances = []
    _FakeSignaling.instances = []
    monkeypatch.setattr(ws, "RTCPeerConnection", _FakePeerConnection)
    monkeypatch.setattr(ws, "PrusaCameraSignaling", _FakeSignaling)
    monkeypatch.setattr(ws, "MediaRelay", lambda: _Relay())
    monkeypatch.setattr(ws, "UPSTREAM_TIMEOUT", 0.2)


class _Relay:
    def subscribe(self, track):  # noqa: ANN001, ANN201
        return track


def make_session(on_closed=None) -> ws.CameraStreamSession:  # noqa: ANN001
    return ws.CameraStreamSession(
        _Api(), "signal.example", "https://cfg", "CAMERATOKEN000000000", "ACCESS",
        on_closed=on_closed,
    )


async def _drive(session, *, track: bool = True, connect: str | None = "connected"):
    """Run ``start`` while feeding the upstream connection its events."""
    task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
    await asyncio.sleep(0)

    upstream = _FakePeerConnection.instances[0]
    if track:
        await upstream.fire("track", _Track())
    if connect is not None:
        await upstream.enter(connect)
    return await task


class _Track:
    kind = "video"


class TestStartup:
    """What has to be true before a viewer is answered."""

    @pytest.mark.asyncio
    async def test_answers_once_the_camera_connects(self) -> None:
        session = make_session()
        answer = await _drive(session)
        assert "ANSWER" in answer

    @pytest.mark.asyncio
    async def test_a_track_alone_is_not_enough(self) -> None:
        """The regression: a track exists long before media can flow."""
        session = make_session()
        with pytest.raises(SignalingError, match="did not connect"):
            await _drive(session, connect=None)

    @pytest.mark.asyncio
    async def test_gives_up_when_no_track_arrives(self) -> None:
        session = make_session()
        with pytest.raises(SignalingError, match="no video track"):
            await _drive(session, track=False, connect=None)

    @pytest.mark.asyncio
    async def test_reports_a_failed_connection_rather_than_waiting(self) -> None:
        """Failure is known immediately; the viewer should not wait it out."""
        session = make_session()
        with pytest.raises(SignalingError):
            await _drive(session, connect="failed")

    @pytest.mark.asyncio
    async def test_a_failed_start_closes_everything(self) -> None:
        session = make_session()
        with pytest.raises(SignalingError):
            await _drive(session, connect=None)

        assert _FakeSignaling.instances[0].closed, "signalling left open"
        assert all(pc.closed for pc in _FakePeerConnection.instances)


class TestTrickle:
    """The camera reads candidates from CANDIDATE messages, not from the SDP.

    It offers ``a=ice-options:ice2,trickle``, and a capture of the working web
    client shows every candidate sent as its own message. Leaving them only in
    the answer means the camera never learns an address to send to: an offer
    arrives, an answer goes back, and ICE sits in checking until it times out.
    """

    @pytest.mark.asyncio
    async def test_every_candidate_is_sent_separately(self) -> None:
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        sent = [c for c, _mid in signaling.candidates]
        assert len(sent) == 3, sent
        assert any("typ host" in c for c in sent)
        assert any("typ srflx" in c for c in sent)
        assert any("typ relay" in c for c in sent)

        upstream = _FakePeerConnection.instances[0]
        await upstream.fire("track", _Track())
        await upstream.enter("connected")
        await task

    @pytest.mark.asyncio
    async def test_a_candidate_repeated_per_m_section_is_sent_once(self) -> None:
        """aiortc lists the same candidates under video and the data channel."""
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        sent = [c for c, _mid in signaling.candidates]
        assert len(sent) == len(set(sent)) == 3, sent

    @pytest.mark.asyncio
    async def test_candidates_use_the_mid_the_camera_expects(self) -> None:
        """Observed on the wire: every candidate carries mid "video-stream"."""
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        assert {mid for _c, mid in signaling.candidates} == {"video-stream"}

    @pytest.mark.asyncio
    async def test_the_answer_still_goes_first(self) -> None:
        """Candidates for an unanswered session have nothing to attach to."""
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        assert signaling.answers, "no answer was sent"
        assert "ANSWER" in signaling.answers[0]

    @pytest.mark.asyncio
    async def test_non_candidate_lines_are_not_sent(self) -> None:
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        for candidate, _mid in signaling.candidates:
            assert candidate.startswith("a=candidate:"), candidate

    @pytest.mark.asyncio
    async def test_one_rejected_candidate_does_not_kill_the_session(self) -> None:
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]

        calls = {"n": 0}

        async def flaky(candidate: str, mid: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("transient")
            signaling.candidates.append((candidate, mid))

        signaling.send_candidate = flaky
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        assert calls["n"] == 3, "gave up after the failure"
        assert len(signaling.candidates) == 2


class TestRelayOnly:
    """Restricting ICE to the relay, for hosts that offer too many addresses.

    Home Assistant runs with host networking, so it offers the camera every
    address the machine has — a LAN address plus two Docker bridges. On the
    same camera minutes apart, the same code connected from a one-interface
    container and failed from Home Assistant with all eight pairs unanswered,
    relay to relay included. This narrows them to the one known to work.
    """

    @pytest.mark.asyncio
    async def test_off_by_default(self, monkeypatch) -> None:
        """A same-network printer is better served by a direct connection."""
        applied: list = []
        monkeypatch.setattr(ws, "_force_relay_only", applied.append)
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        await _FakeSignaling.instances[0].on_offer("v=0\r\nCAMERA\r\n")
        upstream = _FakePeerConnection.instances[0]
        await upstream.fire("track", _Track())
        await upstream.enter("connected")
        await task
        assert applied == []

    @pytest.mark.asyncio
    async def test_applied_when_asked_for(self, monkeypatch) -> None:
        applied: list = []
        monkeypatch.setattr(ws, "_force_relay_only", applied.append)
        session = ws.CameraStreamSession(
            _Api(), "signal.example", "https://cfg", "CAMERATOKEN000000000",
            "ACCESS", relay_only=True,
        )
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        await _FakeSignaling.instances[0].on_offer("v=0\r\nCAMERA\r\n")
        upstream = _FakePeerConnection.instances[0]
        await upstream.fire("track", _Track())
        await upstream.enter("connected")
        await task
        assert len(applied) == 1

    @pytest.mark.asyncio
    async def test_applied_between_the_two_descriptions(self, monkeypatch) -> None:
        """The transports exist only after the remote description, and gathering
        happens during the local one, so there is exactly one usable window."""
        order: list[str] = []
        monkeypatch.setattr(
            ws, "_force_relay_only", lambda pc: order.append("policy")
        )
        session = ws.CameraStreamSession(
            _Api(), "signal.example", "https://cfg", "CAMERATOKEN000000000",
            "ACCESS", relay_only=True,
        )
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        upstream = _FakePeerConnection.instances[0]
        await _FakeSignaling.instances[0].on_offer("v=0\r\nCAMERA\r\n")
        await upstream.fire("track", _Track())
        await upstream.enter("connected")
        await task

        assert upstream.calls == ["setRemoteDescription", "setLocalDescription"]
        assert order == ["policy"]


class TestForceRelayHelper:
    """It reaches into aiortc's internals, so it has to fail softly."""

    class _Conn:
        def __init__(self) -> None:
            self._transport_policy = "ALL"

    class _Gatherer:
        def __init__(self, conn) -> None:  # noqa: ANN001
            self._connection = conn

    class _Ice:
        def __init__(self, gatherer) -> None:  # noqa: ANN001
            self.iceGatherer = gatherer  # noqa: N815

    class _Dtls:
        def __init__(self, ice) -> None:  # noqa: ANN001
            self.transport = ice

    class _Holder:
        def __init__(self, dtls) -> None:  # noqa: ANN001
            self.transport = dtls

    class _Pc:
        def __init__(self, holders) -> None:  # noqa: ANN001
            self._holders = holders
            self.sctp = None

        def getTransceivers(self):  # noqa: ANN201, N802
            return [
                type("T", (), {"receiver": h, "sender": h})() for h in self._holders
            ]

    def _build(self):
        conn = self._Conn()
        holder = self._Holder(self._Dtls(self._Ice(self._Gatherer(conn))))
        return conn, self._Pc([holder])

    def test_sets_the_policy_on_the_connection(self) -> None:
        conn, pc = self._build()
        ws._force_relay_only(pc)
        assert str(conn._transport_policy) == "TransportPolicy.RELAY"

    def test_missing_internals_warn_rather_than_raise(self, caplog) -> None:
        """If aiortc's shape changes, the stream degrades rather than breaks."""
        import logging

        pc = self._Pc([self._Holder(None)])
        with caplog.at_level(logging.WARNING):
            ws._force_relay_only(pc)
        assert "internals have changed" in caplog.text

    def test_a_connection_lacking_the_attribute_is_skipped(self, caplog) -> None:
        import logging

        pc = self._Pc([self._Holder(self._Dtls(self._Ice(self._Gatherer(object()))))])
        with caplog.at_level(logging.WARNING):
            ws._force_relay_only(pc)
        assert "internals have changed" in caplog.text


class TestDiagnosis:
    """A failure the user can act on beats a generic timeout."""

    @pytest.mark.asyncio
    async def test_blames_the_relay_when_we_never_got_one(self) -> None:
        """No relay of our own means no route at all — say so."""
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        upstream = _FakePeerConnection.instances[0]
        upstream.localDescription = _Description(HOST_ONLY_SDP)
        await upstream.fire("track", _Track())
        await upstream.enter("failed")

        with pytest.raises(SignalingError, match="TURN"):
            await task

    @pytest.mark.asyncio
    async def test_blames_the_relay_on_a_timeout_too(self) -> None:
        """The observed case: no relay leaves ICE stuck, it never says "failed"."""
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        upstream = _FakePeerConnection.instances[0]
        upstream.localDescription = _Description(HOST_ONLY_SDP)
        await upstream.fire("track", _Track())

        with pytest.raises(SignalingError, match="TURN"):
            await task

    @pytest.mark.asyncio
    async def test_blames_the_cameras_relay_when_it_has_none(self) -> None:
        """The pair that carries media runs to the camera's relay, not ours.

        When the quota is full the camera goes without and offers only an
        unreachable address, which reads as a local network problem unless the
        message says otherwise.
        """
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        upstream = _FakePeerConnection.instances[0]
        upstream.remoteDescription = _Description(
            "v=0\r\na=candidate:2 1 udp 1 185.87.61.1 58303 typ srflx\r\n"
        )
        await upstream.fire("track", _Track())
        await upstream.enter("failed")

        with pytest.raises(SignalingError, match="camera could not get a relay"):
            await task

    @pytest.mark.asyncio
    async def test_does_not_blame_the_relay_when_we_had_one(self) -> None:
        session = make_session()
        with pytest.raises(SignalingError) as caught:
            await _drive(session, connect="failed")
        assert "TURN" not in str(caught.value)


class TestIceServerOrdering:
    """aioice uses only the first STUN server, so which one is first matters."""

    def test_a_udp_stun_url_goes_first(self) -> None:
        """Measured: :5349 yields no srflx, :3478 does.

        Without a reflexive candidate the only workable pair is relay-to-relay,
        which spends an allocation on a quota-limited server for what should be
        ordinary NAT traversal.
        """
        assert ws._order_urls(
            ["stun:stun.l.google.com:5349", "stun:stun1.l.google.com:3478"]
        ) == ["stun:stun1.l.google.com:3478", "stun:stun.l.google.com:5349"]

    def test_an_already_usable_order_is_kept(self) -> None:
        urls = ["stun:stun1.l.google.com:3478", "stun:stun.l.google.com:5349"]
        assert ws._order_urls(urls) == urls

    def test_turn_is_left_exactly_as_connect_sent_it(self) -> None:
        """aioice does speak TURN over TLS, and turns: first is what works."""
        urls = [
            "turns:coturn.prusa3d.com:5349",
            "turn:coturn.prusa3d.com:3478?transport=udp",
        ]
        assert ws._order_urls(urls) == urls

    def test_the_config_we_build_reorders_stun(self) -> None:
        config = ws._to_aiortc([{"urls": ["stun:a:5349", "stun:b:3478"]}])
        assert config.iceServers[0].urls == ["stun:b:3478", "stun:a:5349"]


class TestWeKeepOurOwnRelay:
    """Dropping our relay stops the camera gathering one, and kills the stream.

    Measured back to back on the same camera, sixty seconds apart: with our
    configuration reduced to STUN the camera offered no candidates at all and
    the connection failed; with TURN restored it offered a relay and delivered
    343 frames. Whatever the camera's reasoning, ours is what makes its relay
    appear.
    """

    def test_turn_survives_in_our_configuration(self) -> None:
        config = ws._to_aiortc(
            [
                {
                    "urls": [
                        "turns:coturn.prusa3d.com:5349",
                        "turn:coturn.prusa3d.com:3478?transport=udp",
                    ],
                    "username": "u",
                    "credential": "c",
                },
                {"urls": ["stun:a:5349", "stun:b:3478"]},
            ]
        )
        every_url = [u for srv in config.iceServers for u in srv.urls]
        assert any("turn" in u for u in every_url), every_url
        assert config.iceServers[0].credential == "c"

    def test_turn_ordering_is_left_exactly_as_connect_sent_it(self) -> None:
        """aioice speaks TURN over TLS, and the turns: URL first is what works."""
        urls = [
            "turns:coturn.prusa3d.com:5349",
            "turn:coturn.prusa3d.com:3478?transport=udp",
        ]
        config = ws._to_aiortc([{"urls": urls, "username": "u"}])
        assert config.iceServers[0].urls == urls

    def test_stun_is_still_reordered(self) -> None:
        config = ws._to_aiortc([{"urls": ["stun:a:5349", "stun:b:3478"]}])
        assert config.iceServers[0].urls == ["stun:b:3478", "stun:a:5349"]


class TestFailureLogging:
    """The diagnosis is gone once the session closes, so record it."""

    def test_summarises_candidate_kinds(self) -> None:
        sdp = (
            "a=candidate:1 1 udp 1 10.0.0.1 1 typ host\r\n"
            "a=candidate:2 1 udp 1 1.2.3.4 1 typ srflx raddr 0.0.0.0\r\n"
            "a=candidate:3 1 udp 1 5.6.7.8 1 typ relay raddr 0.0.0.0\r\n"
        )
        assert ws._candidate_types(sdp) == "host, srflx, relay"

    def test_repeats_are_collapsed(self) -> None:
        sdp = (
            "a=candidate:1 1 udp 1 10.0.0.1 1 typ host\r\n"
            "a=candidate:1 1 udp 1 10.0.0.1 1 typ host\r\n"
        )
        assert ws._candidate_types(sdp) == "host"

    def test_nothing_gathered_reads_as_empty(self) -> None:
        assert ws._candidate_types("v=0\r\nm=video 9\r\n") == ""

    @pytest.mark.asyncio
    async def test_the_failure_is_logged_with_both_sides(self, caplog) -> None:
        import logging

        session = make_session()
        with caplog.at_level(logging.WARNING):
            with pytest.raises(SignalingError):
                await _drive(session, connect="failed")

        assert "We offered" in caplog.text
        assert "the camera offered" in caplog.text


class TestLateFailure:
    """A stream that dies after the viewer is answered."""

    @pytest.mark.asyncio
    async def test_closes_itself_and_reports_it(self) -> None:
        closed: list[bool] = []
        session = make_session(on_closed=lambda: closed.append(True))
        await _drive(session)

        await _FakePeerConnection.instances[0].enter("failed")
        await asyncio.sleep(0)

        assert closed == [True]
        assert _FakeSignaling.instances[0].closed

    @pytest.mark.asyncio
    async def test_a_live_stream_is_left_alone(self) -> None:
        closed: list[bool] = []
        session = make_session(on_closed=lambda: closed.append(True))
        await _drive(session)

        await asyncio.sleep(0)
        assert closed == []
        assert not _FakeSignaling.instances[0].closed
