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
