"""Constants for the Prusa Connect integration."""

from enum import StrEnum
from typing import Final

DOMAIN: Final = "prusa_connect"

# API endpoints
API_BASE_URL: Final = "https://connect-mobile-api.prusa3d.com"
AUTH_AUTHORIZE_URL: Final = "https://account.prusa3d.com/o/authorize/"
AUTH_TOKEN_URL: Final = "https://account.prusa3d.com/o/token/"
AUTH_LOGIN_URL: Final = "https://account.prusa3d.com/login/"
AUTH_CLIENT_ID: Final = "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4"
AUTH_REDIRECT_URI: Final = "https://connect.prusa3d.com/login/auth-callback"
AUTH_SCOPE: Final = "openid"

# Config entry keys
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_USER_ID: Final = "user_id"

# Coordinator settings
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
FAST_SCAN_INTERVAL: Final = 5  # seconds
FAST_SCAN_DURATION: Final = 30  # seconds

# Data keys stored in hass.data[DOMAIN][entry.entry_id]
DATA_API: Final = "api"
DATA_PRINTER_COORDINATOR: Final = "printer_coordinator"
DATA_JOB_COORDINATOR: Final = "job_coordinator"

MANUFACTURER: Final = "Prusa Research"
CONFIGURATION_URL: Final = "https://connect.prusa3d.com"


class PrinterState(StrEnum):
    """Printer states from the Prusa Connect API."""

    IDLE = "IDLE"
    READY = "READY"
    BUSY = "BUSY"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    ATTENTION = "ATTENTION"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class JobState(StrEnum):
    """Job states from the Prusa Connect API."""

    QUEUED = "QUEUED"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"
