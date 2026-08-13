# Prusa Connect camera protocol (reverse-engineered)

Notes for implementing live camera streaming in this integration.

Derived on 2026-08-13 from a sanitized HAR of `connect.prusa3d.com` plus the
lazy-loaded frontend chunk `CamerasView-*.js`. **Nothing here is officially
documented or supported by Prusa**; it can break on any frontend deploy.

## Two transports

| Transport | Endpoint | Latency | Notes |
| --- | --- | --- | --- |
| Snapshot (implemented) | `GET /app/cameras/{camera_id}/snapshots/last?printer_uuid=…` | 30 s printing / 120 s idle | What `camera.py` uses today. Also `GET /thumbnail/camera/{camera_id}`. |
| WebRTC (not implemented) | see below | real time | What the web UI uses for live view. |

The camera itself also has a **Streaming** setting (`Disable` / `RTSP` / `WebRTC`).
`RTSP` exposes `rtsp://<camera-lan-ip>/live`, but it is LAN-only *and* mutually
exclusive with WebRTC — selecting it disables remote viewing. Not usable for a
cloud integration.

## Runtime configuration

Fetched unauthenticated from `https://connect.prusa3d.com/environment.js`:

```js
window.CAMERA_SIGNALING_SERVER  = 'camera-signaling.prusa3d.com'
window.CAMERA_WEBRTC_CONFIG_URL = 'https://camera-service-api.prusa3d.com/v1/camera-webrtc-config'
window.MQTT_BROKER_URL          = 'wss://mqtt.prusa3d.com:8084/mqtt'
window.AI_DETECTOR_API_URL      = 'https://clip-detector.dev.connect.prusa3d.com'
```

Do not hardcode these hosts — read `environment.js` so the integration follows
Prusa's own deployments.

## Handshake

1. `GET $CAMERA_WEBRTC_CONFIG_URL` with `Authorization: Bearer <access_token>`
   (the same OAuth token the integration already holds). Returns ICE servers and
   a `ttl` (observed: 300 s). The client caches this and refetches 30 s before
   expiry, and trims STUN entries to 2 while keeping all TURN servers.
2. Open a Socket.IO (Engine.IO v4) connection to `$CAMERA_SIGNALING_SERVER`
   at `/socket.io/?EIO=4&transport=websocket`, passing the camera token as the
   Socket.IO handshake auth: `40{"token":"<camera_token>"}`.
3. Emit `client_authentication` with an `Auth` payload (binary attachment).
4. Client sends `message_type = REQUEST` carrying its `ice_configuration`.
5. **The server sends the SDP offer** (`message_type = OFFER`). The client is the
   answerer — it never calls `createOffer`.
6. Client applies the offer, calls `createAnswer`, and replies with
   `message_type = ANSWER`.
7. ICE candidates trickle both ways as `message_type = CANDIDATE`. Outgoing
   candidates are prefixed with `a=`; incoming ones have it stripped.
   Messages are correlated by `request_id`.

### Socket.IO events

Every event carries a single binary protobuf attachment (`451-…` frames).

| Event | Direction | Purpose |
| --- | --- | --- |
| `client_authentication` | up | `Auth` — must be first |
| `webrtc` | both | `WebRtcMessage` — the signaling channel |
| `trigger` | up | request a fresh snapshot |
| `configuration` | up | change camera settings |
| `status` | down | camera status |
| `features` | down | capability list |

### OAuth scope — required for WebRTC

**The account token must be issued with the `connect` scope.** The camera
service mints TURN credentials with the username `<expiry>:<connect_id>`, and
`connect_id` is a JWT claim that only appears under that scope. A token issued
with `openid` alone authenticates fine and drives every other feature — but
`/v1/camera-webrtc-config` silently returns **STUN servers only**, and WebRTC
can never establish. There is no error; the offer simply never arrives.

The web app requests `basic_info user_operations email_lists openid connect`.
Scopes are fixed at issue time and a refresh token cannot widen them, so
changing `AUTH_SCOPE` requires users to re-authenticate. Existing installs will
have `openid`-only tokens: detect the missing TURN server in the ICE config and
raise a reauth flow rather than presenting a dead camera.

### Trim the ICE server list

Port the frontend's `Cr()`: keep **every** TURN server but cap STUN at **2 URLs
total** (`Sr = 2`). The config endpoint returns nine Google STUN servers; the
web app never forwards more than two. The camera is an embedded device and the
trimming looks deliberate.

With the right scope and this trimming, our `REQUEST` is byte-identical to the
browser's (272 bytes) apart from the TURN credential itself.

### Credentials

Both are already available to this integration — nothing new to obtain:

- **`camera_token`** — the `token` field of the camera object returned by
  `GET /app/printers/{uuid}/cameras` (already called by `camera.py`). Used both
  as the Socket.IO handshake auth and as field 1 of every message.
- **`client_jwt_token`** — the account OAuth **access token**. Decoding one
  shows `app: connect`, `type: access`, and a `connect_id` claim matching the
  `<expiry>:<connect_id>` TURN username.
- **`client_type`** — the literal string `"client"` (a string, not an enum).

The web client tears the session down when the browser tab loses visibility, so
sessions are inherently short-lived and on-demand.

## Message schema (protobuf)

Payloads are protobuf, not JSON. Field numbers below were read from the
generated encoders; **enum value names are known but their numeric values are
only confirmed for `MessageType`.**

```proto
message WebRtcMessage {            // encoder `ir`
  string           camera_token         = 1;
  string           request_id           = 2;
  string           fingerprint          = 3;
  SdpPayload       sdp                  = 4;
  MessageType      message_type         = 5;
  WebRtcStreamStatus webrtc_stream_status = 6;
  ClientType       client_type          = 7;
  IceConfiguration ice_configuration    = 8;
}

message SdpPayload {               // encoder `tr`
  string candidate = 1;            // full SDP blob for OFFER/ANSWER, or an ICE candidate
  string mid       = 2;            // defaults to "0"
}

message IceConfiguration {         // encoder `Zn`
  repeated IceServer ice_servers          = 1;
  IceTransportPolicy ice_transport_policy = 2;   // POLICY_ALL | POLICY_RELAY
  uint32             ttl                  = 3;
}

message IceServer {                // encoder `Zn`
  repeated Endpoint endpoints  = 1;
  string            username   = 2;
  string            credential = 3;
}

message Endpoint {                 // decoder `Xn`
  SchemeType scheme_type        = 1;   // stun / turn / turns
  string     address            = 2;
  uint32     port               = 3;
  TransportProtocol transport_protocol = 4;   // udp / tcp
}

message Auth {                     // encoder `An`
  string     camera_token     = 1;
  ClientType client_type      = 2;
  string     client_jwt_token = 3;
}

enum MessageType {
  WEBRTC_MSG_TYPE_UNKNOWN   = 0;
  WEBRTC_MSG_TYPE_REQUEST   = 1;
  WEBRTC_MSG_TYPE_ANSWER    = 2;
  WEBRTC_MSG_TYPE_OFFER     = 3;
  WEBRTC_MSG_TYPE_CANDIDATE = 4;
}
```

### Enums (confirmed)

```proto
enum ClientType        { CLIENT_TYPE_UNKNOWN=0;   CLIENT_TYPE_CAMERA=1; CLIENT_TYPE_CLIENT=2; }
enum SchemeType        { SCHEME_TYPE_UNDEFINED=0; SCHEME_TYPE_STUN=1;   SCHEME_TYPE_TURN=2; }
enum TransportProtocol { TRANSPORT_PROTOCOL_UNDEFINED=0; ..._UDP=1; ..._TCP=2; ..._TLS=3; }
enum IceTransportPolicy{ POLICY_UNDEFINED=0;      POLICY_ALL=1;         POLICY_RELAY=2; }
```

### `request_id` must be the Socket.IO namespace sid

The server routes every reply by `request_id`, and it must be the **Socket.IO**
session id — the one in the `40{"sid":…}` CONNECT packet — not the Engine.IO id
from the `0{"sid":…}` OPEN packet. They are different values and both are 20
characters, so they are easy to confuse.

In python-socketio, `client.sid` returns the **Engine.IO** id. Use
`client.namespaces.get("/")` instead:

```python
session_id = sio.namespaces.get("/")   # correct
session_id = sio.sid                   # WRONG - Engine.IO id
```

Getting this wrong is silent: the server accepts the `REQUEST`, returns no
error, and simply never delivers an offer. `fingerprint` carries the same value
for client-originated messages (the camera puts its own hardware fingerprint
there, matching `features` field 7).

### Verified working

With everything above correct, a Python client using `aiortc` completes the
handshake and receives media:

```
Connection(0) ICE completed
connection state : connected
track            : video
frames received  : 428 in 45s
```

The negotiated video codec is **H.264**, so a relay can forward RTP without
transcoding — which is also the codec HomeKit expects.

### Traps that cost real debugging time

- **`client_type` is encoded two different ways.** In `Auth` it is the literal
  string `"client"`; in `WebRtcMessage` it is the `ClientType` varint enum
  (`=2`). Sending the string in `WebRtcMessage` yields the server error
  `webrtc - Error: Unimplemented type: 3` — that message is the protobuf
  skip-unknown-field helper reporting an unhandled **wire type**, not a
  message type. The length prefix gets read as the enum, parsing resumes
  mid-string, and `'c'` (0x63) decodes as field 12 / wire type 3.
- **`POLICY_ALL` is 1, not 0.** Zero means `UNDEFINED`.
- **Endpoints are structured messages, not URL strings.** Port the frontend's
  `br()`: parse `^(stun|turn|turns):([^:?]+)(?::(\d+))?(?:\?transport=(udp|tcp|tls))?$`
  and note that `turns:` implies `TRANSPORT_PROTOCOL_TLS`.
- The SDP blob travels in `sdp.candidate`, the same field used for ICE
  candidate strings. Disambiguate on `message_type`.

Any server-side `Unimplemented type: N` means *our encoding is malformed*, and
N is the wire type it choked on — a fast way to localise the bad field.

## Wider camera command set

The same protobuf family carries a full device API, which suggests future
features beyond live view — `get_status`, `get_features`, `get_snapshot`,
`set_snapshot_enable`, `set_timelapse_enable`, `start_rtsp_server`,
`start_timelapse_video`, `get_timelapse_file_list`, `start_fw_update`,
`start_device_reboot`, `get_protocol_information`.

Config fields seen: `rtsp_server_mode`, `webrtc_mode`, `snapshot_interval`,
`camera_flash`, `camera_mode`, `led`, `volume`, `ftp_server`, `rotation`,
`quality`, plus network/system/sensor telemetry (`wifi`, `ethernet`, `sd_card`,
`mcu_temperature`, `uptime`, `load`, `totalRam`, …).

**`start_timelapse_video` and `get_timelapse_file_list` imply Connect can
produce timelapses server-side.** Worth checking before building our own.

## Open questions

- **Codec negotiated** by the camera (H.264 vs VP8) — decides whether a relay
  can forward RTP untouched or must transcode. `scripts/webrtc_poc.py` exists
  to answer this; it is the one unknown that can still change the design.
- **`Endpoint` encoding.** The frontend splits ICE URLs into a structured
  message (`scheme_type`, `address`, `port`, `transport_protocol`) whose enum
  values are unconfirmed. The PoC sends plain URL strings instead; if the
  server rejects the `REQUEST`, this is the first thing to revisit.
- **Numeric values** for `SchemeType`, `TransportProtocol`, and
  `WebRtcStreamStatus` (only `MessageType` is confirmed).
- **Session limits** — whether Connect caps concurrent viewers per camera, and
  whether an integration session conflicts with a browser one.

## Reference hardware

The camera these notes were captured from, for context on what `features`
gate which behaviour:

```
Niceboy Buddy3D-C1, firmware 3.1.5, 1920x1080, trigger_scheme THIRTY_SEC
features: VideoStream, RtspStream, WebRtc, GetSnapshot, TimelapseEn,
          TimelapseInterval, TimelapseVideoMake, TimelapseFileList, MicroSd,
          IrMode, SpeakerVolume, FanControl, FwUpdate, CameraReboot, McuTemp, …
```

Note this is a third-party camera, not Prusa-branded — so the protocol is not
specific to any one vendor's hardware. Gate features on the `features` list
rather than assuming.

## Integration design sketch

Home Assistant's WebRTC camera API expects the integration to *answer* an offer
from the frontend. Prusa also expects us to answer. Both sides offer, so SDP
cannot simply be relayed — the session has to be terminated on both sides and
the media forwarded between them (e.g. `aiortc` + `MediaRelay`), or bridged out
as RTSP and consumed via go2rtc.

Prefer forwarding without transcoding: an HA host should not be re-encoding
H.264. Confirm the negotiated codec early, since it determines feasibility.
