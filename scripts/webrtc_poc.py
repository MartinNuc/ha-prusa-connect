#!/usr/bin/env python3
"""Standalone proof-of-concept for the Prusa Connect WebRTC camera stream.

Deliberately kept outside the integration: it validates the protocol and answers
the one question that decides the whole design — *which codec* the camera
negotiates, and therefore whether a relay can forward RTP untouched or has to
transcode.

Usage:
    pip install aiohttp aiortc "python-socketio[asyncio_client]"
    export PRUSA_ACCESS_TOKEN=...      # or PRUSA_EMAIL + PRUSA_PASSWORD
    python scripts/webrtc_poc.py

See docs/CAMERA_PROTOCOL.md for how this protocol was derived.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys


import aiohttp
import socketio
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

logging.basicConfig(
    level=os.environ.get("POC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
_LOG = logging.getLogger("poc")

ENVIRONMENT_URL = "https://connect.prusa3d.com/environment.js"
CAMERAS_URL = "https://connect.prusa3d.com/app/cameras"

# WebRtcMessage.message_type
MSG_UNKNOWN, MSG_REQUEST, MSG_ANSWER, MSG_OFFER, MSG_CANDIDATE = range(5)

# Auth.client_type is the literal string "client"; WebRtcMessage.client_type is
# a varint enum. Same concept, two encodings — do not unify them.
CLIENT_TYPE_STR = "client"
CLIENT_TYPE_CLIENT = 2  # CLIENT_TYPE_UNKNOWN=0, CAMERA=1, CLIENT=2

# How long to observe the stream before reporting, in seconds.
OBSERVE_SECONDS = 45

# The camera announces itself with `status` / `features` after we authenticate.
# Sending the WebRTC REQUEST before that arrives gets silently ignored — the
# browser always waits, so we do too.
CAMERA_READY_TIMEOUT = 20

# The camera labels its media section "video-stream", not "0".
MEDIA_MID = "video-stream"

# The web app leaves several seconds between `features` and the WebRTC request.
# Firing in the same millisecond appears to be too eager for the camera.
SETTLE_SECONDS = 5


# --------------------------------------------------------------------------
# Minimal protobuf wire codec.
#
# Only length-delimited and varint fields are needed, so a full protobuf
# dependency would be dead weight here (and in the integration later).
# --------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        out.append(bits | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def pb_str(field: int, value: str | bytes) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return _tag(field, 2) + _varint(len(raw)) + raw


def pb_uint(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def pb_msg(field: int, body: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(body)) + body


def pb_decode(data: bytes) -> dict[int, list]:
    """Decode into {field_number: [values]} — bytes for len-delimited, int for varint."""
    out: dict[int, list] = {}
    i = 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            length, i = _read_varint(data, i)
            value, i = data[i : i + length], i + length
        elif wire == 0:
            value, i = _read_varint(data, i)
        elif wire == 5:
            value, i = int.from_bytes(data[i : i + 4], "little"), i + 4
        elif wire == 1:
            value, i = int.from_bytes(data[i : i + 8], "little"), i + 8
        else:
            raise ValueError(f"unsupported wire type {wire} for field {field}")
        out.setdefault(field, []).append(value)
    return out


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, i


def first(fields: dict[int, list], number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


# --------------------------------------------------------------------------
# Message builders (schema: docs/CAMERA_PROTOCOL.md)
# --------------------------------------------------------------------------


def build_auth(camera_token: str, jwt: str) -> bytes:
    return pb_str(1, camera_token) + pb_str(2, CLIENT_TYPE_STR) + pb_str(3, jwt)


SCHEME_STUN, SCHEME_TURN = 1, 2
TRANSPORT_UDP, TRANSPORT_TCP, TRANSPORT_TLS = 1, 2, 3
POLICY_ALL, POLICY_RELAY = 1, 2

_ICE_URL = re.compile(
    r"^(stun|turn|turns):([^:?]+)(?::(\d+))?(?:\?transport=(udp|tcp|tls))?$"
)


def build_endpoint(url: str) -> bytes:
    """Endpoint{ scheme_type=1, address=2, port=3, transport_protocol=4 }.

    Mirrors the frontend's `br()`. Sending the raw URL string here instead is
    what produced "Unimplemented type: 3" — the server parses this field as a
    nested message, so a bare string is read as a malformed one.
    """
    match = _ICE_URL.match(url)
    if not match:
        return pb_str(2, url)

    scheme, address, port, transport = match.groups()
    out = b""
    if scheme == "stun":
        out += pb_uint(1, SCHEME_STUN)
    elif scheme in ("turn", "turns"):
        out += pb_uint(1, SCHEME_TURN)
    out += pb_str(2, address)
    if port:
        out += pb_uint(3, int(port))
    if transport == "udp":
        out += pb_uint(4, TRANSPORT_UDP)
    elif transport == "tcp":
        out += pb_uint(4, TRANSPORT_TCP)
    elif transport == "tls" or scheme == "turns":
        out += pb_uint(4, TRANSPORT_TLS)
    return out


def build_ice_configuration(config: dict) -> bytes:
    """IceConfiguration{ ice_servers=1, ice_transport_policy=2, ttl=3 }."""
    servers = b""
    for server in config.get("iceServers", []):
        urls = server.get("urls", [])
        urls = urls if isinstance(urls, list) else [urls]
        body = b"".join(pb_msg(1, build_endpoint(url)) for url in urls)
        if server.get("username"):
            body += pb_str(2, server["username"])
        if server.get("credential"):
            body += pb_str(3, server["credential"])
        servers += pb_msg(1, body)
    policy = POLICY_RELAY if config.get("iceTransportPolicy") == "relay" else POLICY_ALL
    return servers + pb_uint(2, policy) + pb_uint(3, int(config.get("ttl", 0)))


# Command message field numbers. Only the read-only queries are exposed here on
# purpose — the same message carries start_fw_update (8), start_device_reboot
# (9) and start_rtsp_server (10), which must never be sent by accident.
CMD_GET_STATUS = 1
CMD_GET_FEATURES = 2


def build_command(camera_token: str, field: int) -> bytes:
    """Command{ <flag>=field, camera_token=11 } — field order matches the frontend."""
    return pb_uint(field, 1) + pb_str(11, camera_token)


def build_webrtc(
    camera_token: str,
    request_id: str,
    message_type: int,
    sdp: str | None = None,
    mid: str = MEDIA_MID,
    ice_configuration: bytes | None = None,
) -> bytes:
    # request_id and fingerprint are both the Socket.IO session id. The server
    # routes replies by it, so a random id gets a silent no-response.
    out = pb_str(1, camera_token) + pb_str(2, request_id) + pb_str(3, request_id)
    if sdp is not None:
        out += pb_msg(4, pb_str(1, sdp) + pb_str(2, mid))
    out += pb_uint(5, message_type) + pb_uint(7, CLIENT_TYPE_CLIENT)
    if ice_configuration is not None:
        out += pb_msg(8, ice_configuration)
    return out


def parse_webrtc(data: bytes) -> dict:
    fields = pb_decode(data)
    sdp_blob = first(fields, 4)
    sdp = mid = None
    if isinstance(sdp_blob, bytes):
        inner = pb_decode(sdp_blob)
        raw = first(inner, 1)
        sdp = raw.decode(errors="replace") if isinstance(raw, bytes) else None
        raw_mid = first(inner, 2)
        mid = raw_mid.decode(errors="replace") if isinstance(raw_mid, bytes) else None
    request_id = first(fields, 2)
    return {
        "request_id": request_id.decode() if isinstance(request_id, bytes) else None,
        "sdp": sdp,
        "mid": mid,
        "message_type": first(fields, 5, MSG_UNKNOWN),
    }


# --------------------------------------------------------------------------
# Prusa Connect REST
# --------------------------------------------------------------------------


async def get_environment(session: aiohttp.ClientSession) -> dict[str, str]:
    """Read the deployment's runtime config rather than hardcoding hosts."""
    async with session.get(ENVIRONMENT_URL) as resp:
        resp.raise_for_status()
        text = await resp.text()
    env = {}
    for line in text.splitlines():
        if line.startswith("window.") and "=" in line:
            key, _, value = line[len("window.") :].partition("=")
            env[key.strip()] = value.strip().strip(";").strip().strip("'\"")
    return env


async def get_access_token(session: aiohttp.ClientSession) -> str:
    token = os.environ.get("PRUSA_ACCESS_TOKEN")
    if token:
        return token

    email, password = os.environ.get("PRUSA_EMAIL"), os.environ.get("PRUSA_PASSWORD")
    if not (email and password):
        sys.exit("Set PRUSA_ACCESS_TOKEN, or PRUSA_EMAIL and PRUSA_PASSWORD.")

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from custom_components.prusa_connect.auth import authenticate  # noqa: PLC0415

    tokens = await authenticate(session, email, password)
    return tokens["access_token"]


async def get_camera(session: aiohttp.ClientSession, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with session.get(CAMERAS_URL, headers=headers) as resp:
        resp.raise_for_status()
        cameras = (await resp.json()).get("cameras", [])

    usable = [c for c in cameras if c.get("registered") and c.get("token")]
    if not usable:
        sys.exit("No registered camera with a token on this account.")

    wanted = os.environ.get("PRUSA_CAMERA_ID")
    if wanted:
        usable = [c for c in usable if str(c.get("id")) == wanted] or usable
    camera = usable[0]

    features = camera.get("features", [])
    _LOG.info("Camera %s (%s)", camera.get("id"), camera.get("name"))
    _LOG.info("  model=%s fw=%s", camera["config"].get("model"), camera["config"].get("firmware"))
    if "WebRtc" not in features:
        _LOG.warning("  camera does not advertise the WebRtc feature — expect failure")
    return camera


MAX_STUN_URLS = 2  # frontend's `Sr`


def trim_ice_servers(servers: list[dict]) -> list[dict]:
    """Port of the frontend's `Cr()`: keep every TURN server, cap STUN at 2 URLs.

    The camera is an embedded device; handing it nine STUN servers appears to be
    more than it will accept. The web app never sends more than two.
    """
    turn, stun = [], []
    for server in servers:
        urls = server.get("urls", [])
        urls = urls if isinstance(urls, list) else [urls]
        (turn if any(u.startswith(("turn:", "turns:")) for u in urls) else stun).append(server)

    out = list(turn)
    used = 0
    for server in stun:
        if used >= MAX_STUN_URLS:
            break
        urls = server.get("urls", [])
        urls = urls if isinstance(urls, list) else [urls]
        keep = urls[: MAX_STUN_URLS - used]
        out.append({**server, "urls": keep})
        used += len(keep)
    return out


async def get_webrtc_config(session: aiohttp.ClientSession, url: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    config = payload.get("configuration", payload)
    if config.get("iceServers"):
        config["iceServers"] = trim_ice_servers(config["iceServers"])
    config["ttl"] = payload.get("ttl", 0)
    return config


def to_aiortc(config: dict) -> RTCConfiguration:
    servers = []
    for server in config.get("iceServers", []):
        urls = server.get("urls", [])
        servers.append(
            RTCIceServer(
                urls=urls if isinstance(urls, list) else [urls],
                username=server.get("username"),
                credential=server.get("credential"),
            )
        )
    return RTCConfiguration(iceServers=servers)


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


async def run() -> int:
    async with aiohttp.ClientSession() as session:
        env = await get_environment(session)
        signaling_host = env.get("CAMERA_SIGNALING_SERVER", "camera-signaling.prusa3d.com")
        config_url = env["CAMERA_WEBRTC_CONFIG_URL"]

        jwt = await get_access_token(session)
        camera = await get_camera(session, jwt)
        camera_token = camera["token"]

        ice_config = await get_webrtc_config(session, config_url, jwt)
        _LOG.info("ICE config: %d server(s), ttl=%ss",
                  len(ice_config.get("iceServers", [])), ice_config.get("ttl"))

    pc = RTCPeerConnection(to_aiortc(ice_config))
    sio = socketio.AsyncClient(logger=False, engineio_logger=False)

    session = {"id": ""}  # filled with the Socket.IO sid once connected
    done = asyncio.Event()
    state = {"track": None, "codec": None, "frames": 0, "answered": False}
    pending_candidates: list[dict] = []

    async def send(message_type: int, **kwargs) -> None:
        payload = build_webrtc(camera_token, session["id"], message_type, **kwargs)
        _LOG.info("-> webrtc type=%d (%d bytes)", message_type, len(payload))
        await sio.emit("webrtc", payload)

    camera_ready = asyncio.Event()

    @sio.on("*")
    async def on_any(event, *args) -> None:  # noqa: ANN001
        """Catch-all: the server may be telling us why it is unhappy."""
        if event in ("status", "features"):
            camera_ready.set()
        for arg in args:
            if isinstance(arg, (bytes, bytearray)):
                _LOG.info("<- %s (%d bytes) fields=%s", event, len(arg),
                          sorted(pb_decode(bytes(arg)).keys()))
                _LOG.debug("   raw=%s", base64.b64encode(bytes(arg)).decode())
            else:
                _LOG.info("<- %s %r", event, arg)

    @sio.event
    async def connect_error(data) -> None:  # noqa: ANN001
        _LOG.error("Socket.IO connect_error: %r", data)

    @sio.event
    async def disconnect() -> None:
        _LOG.warning("Socket.IO disconnected")

    @pc.on("track")
    def on_track(track):  # noqa: ANN001
        _LOG.info("Track received: kind=%s id=%s", track.kind, track.id)
        state["track"] = track
        asyncio.ensure_future(drain(track))

    async def drain(track) -> None:  # noqa: ANN001
        """Pull frames so the pipeline runs, and count them."""
        while True:
            try:
                await track.recv()
            except Exception:  # noqa: BLE001 - track ends when the session closes
                return
            state["frames"] += 1

    @pc.on("connectionstatechange")
    async def on_state_change() -> None:
        _LOG.info("Connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            done.set()

    @sio.on("webrtc")
    async def on_webrtc(data) -> None:  # noqa: ANN001
        if not isinstance(data, (bytes, bytearray)):
            _LOG.warning("Non-binary webrtc payload: %r", data)
            return
        msg = parse_webrtc(bytes(data))

        if msg["message_type"] == MSG_OFFER and not state["answered"]:
            state["answered"] = True
            _LOG.info("Offer received (%d bytes of SDP)", len(msg["sdp"] or ""))
            await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="offer"))
            for pending in pending_candidates:
                await add_candidate(pending)
            pending_candidates.clear()
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await send(MSG_ANSWER, sdp=pc.localDescription.sdp)
            _LOG.info("Answer sent")
            report_codec()

        elif msg["message_type"] == MSG_CANDIDATE:
            if pc.remoteDescription is None:
                pending_candidates.append(msg)
            else:
                await add_candidate(msg)

    async def add_candidate(msg: dict) -> None:
        from aiortc.sdp import candidate_from_sdp  # noqa: PLC0415

        raw = (msg.get("sdp") or "").removeprefix("a=")
        if not raw:
            return
        try:
            candidate = candidate_from_sdp(raw)
            candidate.sdpMid = msg.get("mid") or "0"
            candidate.sdpMLineIndex = 0
            await pc.addIceCandidate(candidate)
        except Exception as err:  # noqa: BLE001 - a bad candidate must not kill the session
            _LOG.debug("Ignoring candidate %r: %s", raw[:60], err)

    def report_codec() -> None:
        """The answer to the question this PoC exists for."""
        for transceiver in pc.getTransceivers():
            if transceiver.receiver and transceiver.receiver.track:
                codecs = getattr(transceiver._offeredCodecs, "__iter__", None)
                if codecs:
                    names = [c.mimeType for c in transceiver._offeredCodecs]
                    state["codec"] = names
                    _LOG.info("NEGOTIATED CODECS: %s", ", ".join(names))

    @pc.on("icecandidate")
    async def on_icecandidate(candidate) -> None:  # noqa: ANN001
        if candidate:
            await send(MSG_CANDIDATE, sdp=f"a={candidate.to_sdp()}", mid=candidate.sdpMid or "0")

    url = f"https://{signaling_host}"
    _LOG.info("Connecting to signaling at %s", url)
    await sio.connect(url, auth={"token": camera_token}, transports=["websocket"])
    # NOT sio.sid — python-socketio exposes the *Engine.IO* sid there. The
    # signaling server routes replies by the *Socket.IO* namespace sid, the one
    # delivered in the `40{"sid":…}` CONNECT packet. Using the wrong one means
    # the request is accepted and then silently dropped, with no error.
    session["id"] = sio.namespaces.get("/") or sio.sid
    _LOG.info("Socket.IO connected (namespace sid=%s, engine.io sid=%s)",
              session["id"], sio.sid)

    auth_payload = build_auth(camera_token, jwt)
    auth_ack: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_auth_ack(*args) -> None:
        """The browser sends this with an ack id and receives 430[0] back."""
        if not auth_ack.done():
            auth_ack.set_result(args)

    _LOG.info("-> client_authentication (%d bytes)", len(auth_payload))
    await sio.emit("client_authentication", auth_payload, callback=on_auth_ack)
    try:
        _LOG.info("<- client_authentication ack: %r", await asyncio.wait_for(auth_ack, 10))
    except asyncio.TimeoutError:
        _LOG.error("No ack for client_authentication - auth likely rejected")

    # The camera does not announce itself; the client asks. Without these the
    # session sits idle and no offer is ever produced.
    for name, field in (("get_status", CMD_GET_STATUS), ("get_features", CMD_GET_FEATURES)):
        payload = build_command(camera_token, field)
        _LOG.info("-> trigger %s (%d bytes)", name, len(payload))
        await sio.emit("trigger", payload)

    try:
        await asyncio.wait_for(camera_ready.wait(), CAMERA_READY_TIMEOUT)
        _LOG.info("Camera is live on the signaling channel")
    except asyncio.TimeoutError:
        _LOG.warning("No status/features within %ss - requesting anyway",
                     CAMERA_READY_TIMEOUT)

    _LOG.info("Settling for %ss before requesting", SETTLE_SECONDS)
    await asyncio.sleep(SETTLE_SECONDS)

    await send(MSG_REQUEST, ice_configuration=build_ice_configuration(ice_config))
    _LOG.info("webrtc REQUEST sent (request_id=%s)", session["id"])

    try:
        await asyncio.wait_for(done.wait(), timeout=OBSERVE_SECONDS)
    except asyncio.TimeoutError:
        pass

    _LOG.info("--- result ---")
    _LOG.info("connection state : %s", pc.connectionState)
    _LOG.info("ICE state        : %s", pc.iceConnectionState)
    _LOG.info("track            : %s", state["track"].kind if state["track"] else "none")
    _LOG.info("frames received  : %d in %ss", state["frames"], OBSERVE_SECONDS)
    _LOG.info("codecs           : %s", state["codec"] or "unknown")

    if state["frames"]:
        _LOG.info("SUCCESS - media is flowing.")
    else:
        _LOG.error("No frames. Check the notes in docs/CAMERA_PROTOCOL.md for likely causes.")

    await pc.close()
    await sio.disconnect()
    return 0 if state["frames"] else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)
