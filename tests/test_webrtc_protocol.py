"""Tests for the camera WebRTC signalling codec.

These are golden tests against payloads captured from the Connect web app. That
matters more than usual here: the protocol is undocumented, so the only real
evidence our encoder is correct is that it reproduces the browser's bytes
exactly. Several of the bugs found while building this — a string where an enum
belonged, an unstructured ICE endpoint, an untrimmed STUN list — were invisible
in isolation and only showed up as a byte-length mismatch.

Identifiers in the captured payloads have been replaced with same-length
placeholders — tokens, session ids, the TURN credential and the camera's LAN
address. Substituting equal-length values leaves every length prefix and the
whole byte layout untouched, so these remain genuine captures for the purpose
of checking the encoder; only the opaque values differ.
"""

from __future__ import annotations

import base64

import pytest

from custom_components.prusa_connect.webrtc_protocol import (
    CMD_GET_FEATURES,
    CMD_GET_STATUS,
    MEDIA_MID,
    ClientType,
    IceTransportPolicy,
    MessageType,
    SchemeType,
    TransportProtocol,
    add_candidate_prefix,
    build_auth,
    build_command,
    build_endpoint,
    build_ice_configuration,
    build_webrtc,
    has_turn_server,
    parse_webrtc,
    pb_decode,
    strip_candidate_prefix,
    trim_ice_servers,
)

CAMERA_TOKEN = "CAMERATOKEN000000000"
SESSION_ID = "SOCKETIOSESSION00000"

# Captured: the two `trigger` commands the web app sends after authenticating.
TRIGGER_GET_STATUS = base64.b64decode("CAFaFENBTUVSQVRPS0VOMDAwMDAwMDAw")
TRIGGER_GET_FEATURES = base64.b64decode("EAFaFENBTUVSQVRPS0VOMDAwMDAwMDAw")

# Captured: the web app's WebRTC REQUEST, 272 bytes.
WEBRTC_REQUEST = base64.b64decode(
    "ChRDQU1FUkFUT0tFTjAwMDAwMDAwMBIUU09DS0VUSU9TRVNTSU9OMDAwMDAaFFNPQ0tFVElP"
    "U0VTU0lPTjAwMDAwKAE4AkLHAQqIAQobCAISEmNvdHVybi5wcnVzYTNkLmNvbRjlKSADChsI"
    "AhISY290dXJuLnBydXNhM2QuY29tGJYbIAEKGwgCEhJjb3R1cm4ucHJ1c2EzZC5jb20Ylhsg"
    "AhIRMTcwMDAwMDAwMDowMDAwMDAaHEVYQU1QTEVUVVJOQ1JFREVOVElBTEFBQUFBQT0KNQoY"
    "CAESEXN0dW4ubC5nb29nbGUuY29tGOUpChkIARISc3R1bjEubC5nb29nbGUuY29tGJYbEAEY"
    "rAI="
)

# Captured: an ICE candidate sent *by the camera*, 159 bytes.
CAMERA_CANDIDATE = base64.b64decode(
    "ChRDQU1FUkFUT0tFTjAwMDAwMDAwMBIUU09DS0VUSU9TRVNTSU9OMDAwMDEaIDAwMDAwMDAw"
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwIksKO2E9Y2FuZGlkYXRlOjEgMSBVRFAgMjEyMjMx"
    "NzgyMyAyMDMuMC4xMTMuMTE0IDM2NTEyIHR5cCBob3N0Egx2aWRlby1zdHJlYW0oBDgB"
)

# The ICE config exactly as /v1/camera-webrtc-config returns it, before
# trimming: nine Google STUN servers plus one TURN server.
RAW_ICE_SERVERS = [
    {
        "urls": [
            "stun:stun.l.google.com:5349",
            "stun:stun1.l.google.com:3478",
            "stun:stun1.l.google.com:5349",
            "stun:stun2.l.google.com:19302",
            "stun:stun2.l.google.com:5349",
            "stun:stun3.l.google.com:3478",
            "stun:stun3.l.google.com:5349",
            "stun:stun4.l.google.com:19302",
            "stun:stun4.l.google.com:5349",
        ]
    },
    {
        "urls": [
            "turns:coturn.prusa3d.com:5349",
            "turn:coturn.prusa3d.com:3478?transport=udp",
            "turn:coturn.prusa3d.com:3478?transport=tcp",
        ],
        "username": "1700000000:000000",
        "credential": "EXAMPLETURNCREDENTIALAAAAAA=",
    },
]


class TestCommands:
    """The `trigger` commands that make the camera announce itself."""

    def test_get_status_matches_capture(self) -> None:
        assert build_command(CAMERA_TOKEN, CMD_GET_STATUS) == TRIGGER_GET_STATUS

    def test_get_features_matches_capture(self) -> None:
        assert build_command(CAMERA_TOKEN, CMD_GET_FEATURES) == TRIGGER_GET_FEATURES

    def test_carries_flag_and_token(self) -> None:
        fields = pb_decode(build_command(CAMERA_TOKEN, CMD_GET_STATUS))
        assert fields[CMD_GET_STATUS] == [1]
        assert fields[11] == [CAMERA_TOKEN.encode()]


class TestIceServerTrimming:
    """Ports of the frontend's `Cr()`."""

    def test_keeps_turn_and_caps_stun_at_two(self) -> None:
        trimmed = trim_ice_servers(RAW_ICE_SERVERS)
        assert len(trimmed) == 2
        # TURN first, with every URL retained.
        assert len(trimmed[0]["urls"]) == 3
        assert trimmed[0]["username"] == "1700000000:000000"
        # STUN trimmed from nine URLs to two.
        assert trimmed[1]["urls"] == [
            "stun:stun.l.google.com:5349",
            "stun:stun1.l.google.com:3478",
        ]

    def test_does_not_mutate_input(self) -> None:
        trim_ice_servers(RAW_ICE_SERVERS)
        assert len(RAW_ICE_SERVERS[0]["urls"]) == 9

    def test_stun_only_config_is_detected(self) -> None:
        """A STUN-only config means the token lacked the `connect` scope."""
        assert has_turn_server(RAW_ICE_SERVERS) is True
        assert has_turn_server([RAW_ICE_SERVERS[0]]) is False
        assert has_turn_server([]) is False


class TestEndpoint:
    """`Endpoint` must be a structured message, not a URL string."""

    @pytest.mark.parametrize(
        ("url", "scheme", "port", "transport"),
        [
            ("stun:stun.l.google.com:5349", SchemeType.STUN, 5349, None),
            (
                "turn:coturn.prusa3d.com:3478?transport=udp",
                SchemeType.TURN,
                3478,
                TransportProtocol.UDP,
            ),
            (
                "turn:coturn.prusa3d.com:3478?transport=tcp",
                SchemeType.TURN,
                3478,
                TransportProtocol.TCP,
            ),
            # `turns:` implies TLS even without an explicit transport.
            (
                "turns:coturn.prusa3d.com:5349",
                SchemeType.TURN,
                5349,
                TransportProtocol.TLS,
            ),
        ],
    )
    def test_parses_url_forms(self, url, scheme, port, transport) -> None:
        fields = pb_decode(build_endpoint(url))
        assert fields[1] == [scheme]
        assert fields[3] == [port]
        if transport is None:
            assert 4 not in fields
        else:
            assert fields[4] == [transport]

    def test_unparseable_url_falls_back_to_address(self) -> None:
        fields = pb_decode(build_endpoint("not-a-url"))
        assert fields[2] == [b"not-a-url"]


class TestWebRtcRequest:
    """The message that asks the camera to start streaming."""

    def test_reproduces_captured_request_byte_for_byte(self) -> None:
        """End-to-end: raw config -> trim -> encode == the browser's bytes."""
        payload = build_webrtc(
            CAMERA_TOKEN,
            SESSION_ID,
            MessageType.REQUEST,
            ice_configuration=build_ice_configuration(
                trim_ice_servers(RAW_ICE_SERVERS), "all", 300
            ),
        )
        assert payload == WEBRTC_REQUEST

    def test_request_id_and_fingerprint_are_the_session_id(self) -> None:
        """Both carry the Socket.IO namespace sid; replies route by it."""
        fields = pb_decode(WEBRTC_REQUEST)
        assert fields[2] == [SESSION_ID.encode()]
        assert fields[3] == [SESSION_ID.encode()]

    def test_client_type_is_a_varint_enum(self) -> None:
        """Encoding this as a string makes the server reject the message."""
        fields = pb_decode(WEBRTC_REQUEST)
        assert fields[7] == [ClientType.CLIENT]

    def test_policy_all_is_one(self) -> None:
        """Zero means UNDEFINED, which is not the same thing."""
        config = build_ice_configuration([], "all", 300)
        assert pb_decode(config)[2] == [IceTransportPolicy.ALL]
        config = build_ice_configuration([], "relay", 300)
        assert pb_decode(config)[2] == [IceTransportPolicy.RELAY]


class TestAnswerAndCandidates:
    """Messages we send once the camera has offered."""

    def test_answer_carries_sdp_and_media_mid(self) -> None:
        parsed = parse_webrtc(
            build_webrtc(CAMERA_TOKEN, SESSION_ID, MessageType.ANSWER, sdp="v=0\r\n")
        )
        assert parsed["message_type"] == MessageType.ANSWER
        assert parsed["sdp"] == "v=0\r\n"
        assert parsed["mid"] == MEDIA_MID

    def test_candidate_prefix_round_trips(self) -> None:
        raw = "candidate:1 1 UDP 2122317823 203.0.113.114 36512 typ host"
        assert add_candidate_prefix(raw) == f"a={raw}"
        assert strip_candidate_prefix(f"a={raw}") == raw
        # Idempotent in both directions.
        assert add_candidate_prefix(f"a={raw}") == f"a={raw}"
        assert strip_candidate_prefix(raw) == raw


class TestParsingCameraMessages:
    """Messages the camera sends us."""

    def test_parses_captured_candidate(self) -> None:
        parsed = parse_webrtc(CAMERA_CANDIDATE)
        assert parsed["message_type"] == MessageType.CANDIDATE
        assert parsed["camera_token"] == CAMERA_TOKEN
        assert parsed["request_id"] == "SOCKETIOSESSION00001"
        assert parsed["mid"] == MEDIA_MID
        assert parsed["sdp"] == (
            "a=candidate:1 1 UDP 2122317823 203.0.113.114 36512 typ host"
        )

    def test_camera_identifies_itself_by_hardware_fingerprint(self) -> None:
        """The camera's fingerprint, not a session id — it matches `features`."""
        assert parse_webrtc(CAMERA_CANDIDATE)["fingerprint"] == (
            "00000000000000000000000000000000"
        )

    def test_unknown_fields_do_not_break_parsing(self) -> None:
        """Firmware may add fields; that must not be fatal."""
        from custom_components.prusa_connect.webrtc_protocol import pb_uint

        parsed = parse_webrtc(CAMERA_CANDIDATE + pb_uint(99, 1234))
        assert parsed["message_type"] == MessageType.CANDIDATE


class TestAuth:
    """The first message on the socket."""

    def test_client_type_is_the_literal_string(self) -> None:
        """Unlike WebRtcMessage, Auth uses a string here."""
        fields = pb_decode(build_auth("tok", "jwt"))
        assert fields[1] == [b"tok"]
        assert fields[2] == [b"client"]
        assert fields[3] == [b"jwt"]
