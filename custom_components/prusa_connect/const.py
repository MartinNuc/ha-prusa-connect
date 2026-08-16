"""Constants for the Prusa Connect integration."""

from enum import StrEnum
from typing import Final

DOMAIN: Final = "prusa_connect"

# API endpoints.
# The Connect app API lives on connect.prusa3d.com under /app and accepts the
# Prusa Account access token as a bearer token. (connect-mobile-api.prusa3d.com
# is a separate gateway that rejects tokens issued to this OAuth client.)
API_BASE_URL: Final = "https://connect.prusa3d.com"
API_PREFIX: Final = "/app"
AUTH_AUTHORIZE_URL: Final = "https://account.prusa3d.com/o/authorize/"
AUTH_TOKEN_URL: Final = "https://account.prusa3d.com/o/token/"
AUTH_LOGIN_URL: Final = "https://account.prusa3d.com/login/"
AUTH_CLIENT_ID: Final = "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4"
AUTH_REDIRECT_URI: Final = "https://connect.prusa3d.com/login/auth-callback"
# "openid" alone yields a token with no `connect_id` claim. The camera service
# mints TURN credentials as `<expiry>:<connect_id>`, so a narrow scope silently
# returns STUN-only ICE config and WebRTC never establishes. This mirrors the
# scope the Connect web app requests; trim it once we know the minimum that
# still produces `connect_id`.
AUTH_SCOPE: Final = "basic_info user_operations email_lists openid connect"

# Camera streaming. Connect publishes the camera hosts in a runtime document
# rather than fixing them, so read them from there and treat these as fallbacks.
ENVIRONMENT_URL: Final = f"{API_BASE_URL}/environment.js"
ENVIRONMENT_DEFAULTS: Final = {
    "CAMERA_SIGNALING_SERVER": "camera-signaling.prusa3d.com",
    "CAMERA_WEBRTC_CONFIG_URL": (
        "https://camera-service-api.prusa3d.com/v1/camera-webrtc-config"
    ),
}

# Config entry keys
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_USER_ID: Final = "user_id"

# Coordinator settings
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
FAST_SCAN_INTERVAL: Final = 5  # seconds
FAST_SCAN_DURATION: Final = 30  # seconds

# Timelapse. Recording writes hundreds of megabytes to disk over a long print,
# so it stays off until asked for.
# Connect's own view of the link to the printer, which is not the same thing as
# the print state: a printer that vanishes mid-job keeps reporting the state it
# was last in.
CONNECT_STATE_OFFLINE: Final = "OFFLINE"

CONF_TIMELAPSE: Final = "timelapse"
DEFAULT_TIMELAPSE: Final = False

# The camera uploads a fresh snapshot every 30 seconds under the common
# THIRTY_SEC trigger scheme. Sampling faster only re-fetches the same JPEG
# unless the camera is poked with a `get_snapshot` trigger, which costs upload
# bandwidth wherever the printer lives — so match its own cadence.
TIMELAPSE_INTERVAL: Final = 30  # seconds

# 10 fps turns a 5-hour print into a minute of video.
TIMELAPSE_FPS: Final = 10

# ~24 hours at the sampling interval above. A guard against a print that never
# reports finishing quietly filling the disk, not a real limit.
TIMELAPSE_MAX_FRAMES: Final = 2880

# Videos land in the media library; frames are transient and stay out of it, so
# browsing media never shows thousands of JPEGs.
TIMELAPSE_MEDIA_DIR: Final = "prusa_connect"
TIMELAPSE_WORK_DIR: Final = ".prusa_connect_timelapse"

MANUFACTURER: Final = "Prusa Research"
CONFIGURATION_URL: Final = "https://connect.prusa3d.com"


class PrinterState(StrEnum):
    """Printer states reported by Connect in `printer_state`."""

    IDLE = "IDLE"
    READY = "READY"
    BUSY = "BUSY"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    ATTENTION = "ATTENTION"
    MANIPULATING = "MANIPULATING"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


# Command names and the states they may be issued from, as reported by
# GET /app/printers/{uuid}/supported-commands.
CMD_PAUSE: Final = "PAUSE_PRINT"
CMD_RESUME: Final = "RESUME_PRINT"
CMD_STOP: Final = "STOP_PRINT"
CMD_SET_READY: Final = "SET_PRINTER_READY"
CMD_CANCEL_READY: Final = "CANCEL_PRINTER_READY"
CMD_START_PRINT: Final = "START_PRINT"
CMD_DIALOG_ACTION: Final = "DIALOG_ACTION"

CMD_STATES: Final[dict[str, frozenset[str]]] = {
    CMD_PAUSE: frozenset({"PRINTING"}),
    CMD_RESUME: frozenset({"PAUSED"}),
    CMD_STOP: frozenset({"PAUSED", "PRINTING", "ATTENTION"}),
    CMD_SET_READY: frozenset({"STOPPED", "IDLE", "FINISHED", "READY"}),
    CMD_CANCEL_READY: frozenset({"READY"}),
    CMD_START_PRINT: frozenset({"IDLE", "READY", "FINISHED", "STOPPED"}),
}
