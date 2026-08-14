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

- **`WebRtcStreamStatus` values** — the field is never populated in anything
  captured so far, and we never send it.
- **Session limits** — whether Connect caps concurrent viewers per camera, and
  whether an integration session conflicts with a browser one.
- **Minimum OAuth scope** that still yields `connect_id`. We request what the
  web app requests; `openid connect` alone may well be enough, and asking for
  `email_lists` in a printer integration is hard to justify.
- **The `configuration` event** (28–30 bytes, client to server) appears in some
  captures and not others, and streaming works without it. Purpose unknown.

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

## Integration design

### What is implemented

- `camera.py` — the Home Assistant entity. Serves snapshots always, and live
  WebRTC video for cameras advertising the `WebRtc` feature. Sessions are
  created per viewer and closed when the viewer leaves or the entity is
  removed.
- `webrtc_session.py` — bridges one viewer to one camera: terminates WebRTC on
  both sides and forwards media across.
- `scripts/bridge_probe.py` — drives the bridge with a local aiortc peer in
  place of the HA frontend. Verified live: video reaches the viewer.


- `webrtc_protocol.py` — the wire codec and message builders. Pure functions,
  no I/O, tested byte-for-byte against captured payloads.
- `signaling.py` — the Socket.IO session: authenticate, wake the camera, ask
  for a stream, shuttle SDP and candidates. Knows nothing about WebRTC itself,
  so the media layer stays swappable.
- `api.get_webrtc_config()` — fetches ICE servers, trims them, and raises
  `ConfigEntryAuthFailed` when no TURN server comes back (the reauth signal for
  the scope problem above).
- `scripts/webrtc_poc.py` — a live check that imports the modules above, so a
  successful run validates what ships rather than a parallel copy.

Verified against the live service: `codecs: H264`, ~665 RTP packets in 20 s.

### The media path — still to decide

This is the open design question, and it is not a detail.

Home Assistant's WebRTC camera API expects the integration to **answer** an
offer from the frontend. Prusa's camera also **offers**, expecting us to
answer. Both sides offer, so SDP cannot simply be relayed. The session has to
be terminated on both sides and media forwarded between them.

The catch is cost. `aiortc`'s `MediaRelay` relays *decoded* frames, so
forwarding through it re-encodes 1080p H.264 for every viewer — exactly the
load a Home Assistant host should not take on. Since the camera already speaks
H.264, which is also what HomeKit wants, a passthrough is possible in principle
and worth real effort to achieve.

**What ships today is option 3, `MediaRelay`.** It uses only aiortc's public API
and works: `scripts/bridge_probe.py` gets video through to a stand-in viewer.
But the re-encode is not free — measured on a modest host, roughly 12 fps
arrived from the camera and roughly 6 fps reached the viewer. Acceptable for
glancing at a print, and the resolution is configurable in the Connect web UI,
so anyone raising it will feel this sooner. Moving to passthrough is the fix.

Options, roughly in order of preference:

1. **RTP passthrough via aiortc** — intercept encoded frames before the
   decoder and republish without re-encoding. Cheapest at runtime. There is no
   public API, but the interception point is well defined (below).
2. **Bridge to RTSP, consume via go2rtc** — run the signalling client as a
   go2rtc `exec:` source that emits RTSP. go2rtc starts it only when a
   consumer connects, which gives on-demand behaviour for free and makes
   HomeKit-via-Scrypted and recording trivial. Builds on option 1 for the
   frames.
3. **Transcode and accept the cost** — simplest, and viable for a single
   viewer on capable hardware. Should be measured before being ruled in or out.

#### Encoded-frame passthrough — verified

Passthrough works, so option 1 is available and nothing needs transcoding.

`RTCRtpReceiver` reassembles a complete encoded frame and only then hands it to
a decoder thread, which obtains its decoder from the module-level
`aiortc.rtcrtpreceiver.get_decoder`. Substituting that factory yields the
encoded frames and decodes nothing:

```python
class CapturingDecoder:
    def decode(self, encoded_frame):      # JitterFrame(data: bytes, timestamp: int)
        sink.append(bytes(encoded_frame.data))
        return []                          # nothing to hand onward

aiortc.rtcrtpreceiver.get_decoder = lambda codec: CapturingDecoder()
```

Prefer this seam over the receiver's private `__decoder_queue`: it is a plain
module attribute, so it survives more aiortc versions and is far easier to
assert on. Note the frames are only produced while a decoder thread exists, so
suppressing decoding by other means does not work — the frames are dropped.

`scripts/passthrough_probe.py` does this against the live camera and parses the
result back with PyAV. Measured over 10 s:

```
119 frames, 325 KiB (~11.9 fps, ~266 kbit/s)
codec h264, 640x480, 113 of 119 frames decoded
```

The frames that do not decode are the leading ones, captured before the first
parameter sets and keyframe arrive — expected when joining a stream mid-GOP.
A real implementation should hold frames until the first keyframe.

#### Stream resolution is lower than the snapshots

The live stream is **640x480**, while `/snapshots/last` and the camera's
advertised resolution are 1920x1080. Whether that is fixed, adaptive, or
selectable is unknown — the camera does advertise `VideoQuality` and
`TurnVideoQualityChange`, and the unexplained `configuration` message the web
app sometimes sends is a plausible way it gets set. Worth investigating before
promising HomeKit users a 1080p feed.

Whichever is chosen, sessions should be held only while somebody is watching:
the web app tears its own down when the tab is hidden, and the stream is
relayed through Prusa's TURN infrastructure.

### Session behaviour

- Ask for `get_status` / `get_features` and wait for a reply before requesting
  a stream. The camera does not announce itself.
- Wait for the `client_authentication` ack before sending anything else.
- ICE credentials carry a 300 s TTL; refresh before it expires on long sessions.
- Whether Connect caps concurrent viewers per camera is untested. A browser
  session open at the same time did not visibly block a second one, but this
  deserves confirmation before release.
