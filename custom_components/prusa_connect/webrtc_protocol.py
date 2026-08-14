"""Wire protocol for Prusa Connect camera WebRTC signalling.

Signalling runs over Socket.IO with protobuf payloads. Prusa publish neither a
``.proto`` nor any documentation, so the schema here was reverse-engineered from
the Connect frontend bundle and verified against the live service — see
``docs/CAMERA_PROTOCOL.md`` for the derivation and the traps.

Only length-delimited and varint fields are needed, so this hand-rolls the few
lines of wire codec rather than taking a protobuf dependency.

This module is deliberately pure: no I/O, no Home Assistant imports. That keeps
it testable against captured payloads, which is the only practical way to check
an undocumented protocol.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any, Final


class MessageType(IntEnum):
    """``WebRtcMessage.message_type``."""

    UNKNOWN = 0
    REQUEST = 1
    ANSWER = 2
    OFFER = 3
    CANDIDATE = 4


class ClientType(IntEnum):
    """``WebRtcMessage.client_type``."""

    UNKNOWN = 0
    CAMERA = 1
    CLIENT = 2


class SchemeType(IntEnum):
    """``Endpoint.scheme_type``."""

    UNDEFINED = 0
    STUN = 1
    TURN = 2


class TransportProtocol(IntEnum):
    """``Endpoint.transport_protocol``."""

    UNDEFINED = 0
    UDP = 1
    TCP = 2
    TLS = 3


class IceTransportPolicy(IntEnum):
    """``IceConfiguration.ice_transport_policy``. Note ALL is 1, not 0."""

    UNDEFINED = 0
    ALL = 1
    RELAY = 2


class VideoQuality(IntEnum):
    """``Video.quality`` — the live stream's resolution tier.

    Distinct from the camera's snapshot resolution, which stays 1920x1080
    whatever this is set to. Cameras come up in SD, so a 1080p stream has to be
    asked for explicitly.
    """

    INVALID = 0
    SD = 1
    HD = 2
    FHD = 3


# ``Auth.client_type`` is the literal string "client" while
# ``WebRtcMessage.client_type`` is the ClientType enum. Same concept, two
# encodings — sending the string where the enum belongs makes the server report
# "Unimplemented type: 3".
AUTH_CLIENT_TYPE: Final = "client"

# The camera labels its media section "video-stream", not "0".
MEDIA_MID: Final = "video-stream"

# Socket.IO event names.
EVENT_AUTH: Final = "client_authentication"
EVENT_TRIGGER: Final = "trigger"
EVENT_WEBRTC: Final = "webrtc"
EVENT_STATUS: Final = "status"
EVENT_FEATURES: Final = "features"
EVENT_CONFIGURATION: Final = "configuration"

# Cameras that let the stream quality be changed advertise this. Without it,
# sending a Configuration is at best ignored.
FEATURE_VIDEO_QUALITY: Final = "VideoQuality"

# Command field numbers. Only the read-only queries are exposed: the same
# message also carries start_fw_update (8), start_device_reboot (9) and
# start_rtsp_server (10), which must never be sent by accident.
CMD_GET_STATUS: Final = 1
CMD_GET_FEATURES: Final = 2

# The frontend caps STUN at two URLs while keeping every TURN server. The
# camera is an embedded device and the trimming looks deliberate.
MAX_STUN_URLS: Final = 2

_ICE_URL: Final = re.compile(
    r"^(stun|turn|turns):([^:?]+)(?::(\d+))?(?:\?transport=(udp|tcp|tls))?$"
)


# --------------------------------------------------------------------------
# protobuf wire codec
# --------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    """Encode an unsigned varint."""
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        out.append(bits | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    """Decode a varint, returning ``(value, next_index)``."""
    shift = result = 0
    while True:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, index


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def pb_bytes(field: int, value: str | bytes) -> bytes:
    """Encode a length-delimited field."""
    raw = value.encode() if isinstance(value, str) else value
    return _tag(field, 2) + _varint(len(raw)) + raw


def pb_uint(field: int, value: int) -> bytes:
    """Encode a varint field."""
    return _tag(field, 0) + _varint(value)


def pb_message(field: int, body: bytes) -> bytes:
    """Encode a nested message field."""
    return _tag(field, 2) + _varint(len(body)) + body


def pb_decode(data: bytes) -> dict[int, list[Any]]:
    """Decode into ``{field_number: [values]}``.

    Length-delimited fields yield ``bytes``; varints yield ``int``. Unknown
    fields are preserved rather than raising, so a firmware that adds fields
    does not break parsing.
    """
    out: dict[int, list[Any]] = {}
    index = 0
    while index < len(data):
        tag, index = _read_varint(data, index)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            length, index = _read_varint(data, index)
            value, index = data[index : index + length], index + length
        elif wire == 0:
            value, index = _read_varint(data, index)
        elif wire == 5:
            value, index = int.from_bytes(data[index : index + 4], "little"), index + 4
        elif wire == 1:
            value, index = int.from_bytes(data[index : index + 8], "little"), index + 8
        else:
            # Wire types 3/4 (deprecated groups) never appear in this protocol;
            # seeing one means the stream is misaligned, so stop rather than
            # emit garbage.
            break
        out.setdefault(field, []).append(value)
    return out


def pb_first(fields: dict[int, list[Any]], number: int, default: Any = None) -> Any:
    """Return the first value for ``number``, or ``default``."""
    values = fields.get(number)
    return values[0] if values else default


def _as_text(value: Any) -> str | None:
    """Decode a length-delimited value as text, if it is one."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode(errors="replace")
    return None


# --------------------------------------------------------------------------
# message builders
# --------------------------------------------------------------------------


def build_auth(camera_token: str, access_token: str) -> bytes:
    """``Auth{ camera_token=1, client_type=2, client_jwt_token=3 }``.

    ``client_jwt_token`` is the account access token the integration already
    holds; it must carry a ``connect_id`` claim (see ``AUTH_SCOPE``).
    """
    return (
        pb_bytes(1, camera_token)
        + pb_bytes(2, AUTH_CLIENT_TYPE)
        + pb_bytes(3, access_token)
    )


def build_command(camera_token: str, field: int) -> bytes:
    """``Command{ <flag>=field, camera_token=11 }``.

    Field order matches the frontend's encoder.
    """
    return pb_uint(field, 1) + pb_bytes(11, camera_token)


def build_video_configuration(camera_token: str, quality: VideoQuality) -> bytes:
    """``Configuration{ camera_token=6, video=8 }`` with ``Video{ quality=1 }``.

    The setter for live stream resolution. `Configuration` also carries image
    (1), timelapse_interval (2), control (3) and logs (5); those are left out,
    since the camera applies whatever fields are present.

    The empty `system` submessage is not: every settings call in the Connect
    frontend sends `{system: {}, camera_token, ...}`, so it is reproduced here
    rather than assumed to be incidental.
    """
    return (
        pb_message(4, b"")
        + pb_bytes(6, camera_token)
        + pb_message(8, pb_uint(1, quality))
    )


def build_endpoint(url: str) -> bytes:
    """``Endpoint{ scheme_type=1, address=2, port=3, transport_protocol=4 }``.

    Ports the frontend's ``br()``. This must be a structured message: sending
    the bare URL string makes the server fail to parse it.
    """
    match = _ICE_URL.match(url)
    if not match:
        return pb_bytes(2, url)

    scheme, address, port, transport = match.groups()
    out = b""
    if scheme == "stun":
        out += pb_uint(1, SchemeType.STUN)
    elif scheme in ("turn", "turns"):
        out += pb_uint(1, SchemeType.TURN)
    out += pb_bytes(2, address)
    if port:
        out += pb_uint(3, int(port))
    if transport == "udp":
        out += pb_uint(4, TransportProtocol.UDP)
    elif transport == "tcp":
        out += pb_uint(4, TransportProtocol.TCP)
    elif transport == "tls" or scheme == "turns":
        out += pb_uint(4, TransportProtocol.TLS)
    return out


def _urls_of(server: dict) -> list[str]:
    urls = server.get("urls", [])
    return urls if isinstance(urls, list) else [urls]


def has_turn_server(ice_servers: list[dict]) -> bool:
    """Whether any server is a TURN relay.

    The camera service only issues TURN credentials for tokens carrying a
    ``connect_id`` claim. A STUN-only config therefore means the account token
    was issued with too narrow a scope, and WebRTC cannot establish.
    """
    return any(
        url.startswith(("turn:", "turns:"))
        for server in ice_servers
        for url in _urls_of(server)
    )


def trim_ice_servers(ice_servers: list[dict]) -> list[dict]:
    """Keep every TURN server, cap STUN at ``MAX_STUN_URLS`` URLs total.

    Ports the frontend's ``Cr()``. The config endpoint returns nine Google STUN
    servers; the web app never forwards more than two.
    """
    turn = [s for s in ice_servers if any(u.startswith(("turn:", "turns:")) for u in _urls_of(s))]
    stun = [s for s in ice_servers if s not in turn]

    out = list(turn)
    used = 0
    for server in stun:
        if used >= MAX_STUN_URLS:
            break
        keep = _urls_of(server)[: MAX_STUN_URLS - used]
        out.append({**server, "urls": keep})
        used += len(keep)
    return out


def build_ice_configuration(
    ice_servers: list[dict], policy: str | None, ttl: int
) -> bytes:
    """``IceConfiguration{ ice_servers=1, ice_transport_policy=2, ttl=3 }``."""
    servers = b""
    for server in ice_servers:
        body = b"".join(pb_message(1, build_endpoint(u)) for u in _urls_of(server))
        if server.get("username"):
            body += pb_bytes(2, server["username"])
        if server.get("credential"):
            body += pb_bytes(3, server["credential"])
        servers += pb_message(1, body)

    transport = (
        IceTransportPolicy.RELAY if policy == "relay" else IceTransportPolicy.ALL
    )
    return servers + pb_uint(2, transport) + pb_uint(3, int(ttl))


def build_webrtc(
    camera_token: str,
    session_id: str,
    message_type: MessageType,
    *,
    sdp: str | None = None,
    mid: str = MEDIA_MID,
    ice_configuration: bytes | None = None,
) -> bytes:
    """``WebRtcMessage`` — the signalling channel.

    ``session_id`` is the **Socket.IO namespace** session id, which the server
    uses to route replies. Using the Engine.IO id instead is accepted and then
    silently dropped, with no error and no offer.
    """
    out = (
        pb_bytes(1, camera_token)
        + pb_bytes(2, session_id)
        + pb_bytes(3, session_id)
    )
    if sdp is not None:
        out += pb_message(4, pb_bytes(1, sdp) + pb_bytes(2, mid))
    out += pb_uint(5, message_type) + pb_uint(7, ClientType.CLIENT)
    if ice_configuration is not None:
        out += pb_message(8, ice_configuration)
    return out


def parse_webrtc(data: bytes) -> dict[str, Any]:
    """Parse a ``WebRtcMessage``.

    The SDP blob and ICE candidate strings share one field; disambiguate on
    ``message_type``.
    """
    fields = pb_decode(data)

    sdp = mid = None
    blob = pb_first(fields, 4)
    if isinstance(blob, (bytes, bytearray)):
        inner = pb_decode(bytes(blob))
        sdp = _as_text(pb_first(inner, 1))
        mid = _as_text(pb_first(inner, 2))

    return {
        "camera_token": _as_text(pb_first(fields, 1)),
        "request_id": _as_text(pb_first(fields, 2)),
        "fingerprint": _as_text(pb_first(fields, 3)),
        "sdp": sdp,
        "mid": mid,
        "message_type": pb_first(fields, 5, MessageType.UNKNOWN),
    }


def strip_candidate_prefix(candidate: str) -> str:
    """Incoming candidates carry an ``a=`` prefix the WebRTC stack does not want."""
    return candidate[2:] if candidate.startswith("a=") else candidate


def add_candidate_prefix(candidate: str) -> str:
    """Outgoing candidates must carry the ``a=`` prefix."""
    return candidate if candidate.startswith("a=") else f"a={candidate}"
