"""API client behaviour: URLs, envelopes, command payloads and token refresh."""

from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.prusa_connect.api import PrusaConnectAPI
from custom_components.prusa_connect.const import API_BASE_URL, API_PREFIX


class _Response:
    """Minimal aiohttp-like response."""

    def __init__(self, status=200, payload=None, body=b""):
        self.status = status
        self._payload = payload
        self._body = body

    async def json(self):
        return self._payload

    async def read(self):
        return self._body

    async def text(self):
        return json.dumps(self._payload) if self._payload is not None else ""

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError(f"unexpected HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Records requests and replays queued responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.responses.pop(0)


def _api(session, **kwargs):
    return PrusaConnectAPI(session, "ACCESS", "REFRESH", **kwargs)


def test_printers_url_and_envelope(printers):
    """Collections are unwrapped from their single-key envelope."""
    session = _Session(_Response(payload={"printers": printers}))
    result = asyncio.run(_api(session).get_printers())

    assert session.calls[0]["url"] == f"{API_BASE_URL}{API_PREFIX}/printers"
    assert result == printers


def test_missing_envelope_key_yields_empty_list():
    """An unexpected body shape must not raise."""
    session = _Session(_Response(payload={}))
    assert asyncio.run(_api(session).get_printers()) == []


def test_bearer_token_is_sent():
    """Requests authenticate with the Prusa Account access token."""
    session = _Session(_Response(payload={"printers": []}))
    asyncio.run(_api(session).get_printers())
    assert session.calls[0]["headers"]["Authorization"] == "Bearer ACCESS"


@pytest.mark.parametrize(
    ("method_name", "expected_command"),
    [
        ("pause_print", "PAUSE_PRINT"),
        ("resume_print", "RESUME_PRINT"),
        ("stop_print", "STOP_PRINT"),
        ("set_ready", "SET_PRINTER_READY"),
        ("set_unready", "CANCEL_PRINTER_READY"),
    ],
)
def test_commands_use_api_names(method_name, expected_command, supported_commands):
    """Command names must match what the printer advertises."""
    session = _Session(_Response(status=204))
    asyncio.run(getattr(_api(session), method_name)("UUID"))

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/app/printers/UUID/commands")
    assert call["json"] == {"command": expected_command, "kwargs": {}}
    assert expected_command in {c["command"] for c in supported_commands}


def test_command_arguments_go_in_kwargs():
    """Arguments are nested under `kwargs`, as the web app sends them."""
    session = _Session(_Response(status=204))
    asyncio.run(_api(session).start_print("UUID", "/usb/A.BGC"))

    assert session.calls[0]["json"] == {
        "command": "START_PRINT",
        "kwargs": {"path": "/usb/A.BGC"},
    }


def test_dialog_response_payload():
    """Dialog replies carry both declared arguments."""
    session = _Session(_Response(status=204))
    asyncio.run(_api(session).respond_to_dialog("UUID", 7, "YES"))

    assert session.calls[0]["json"] == {
        "command": "DIALOG_ACTION",
        "kwargs": {"dialog_id": 7, "button": "YES"},
    }


def test_401_triggers_refresh_then_retries(monkeypatch):
    """An expired token is refreshed once and the request replayed."""
    session = _Session(
        _Response(status=401),
        _Response(payload={"printers": []}),
    )
    persisted = {}

    async def fake_refresh(_session, _refresh_token):
        return {"access_token": "NEW", "refresh_token": "NEWREFRESH"}

    monkeypatch.setattr(
        "custom_components.prusa_connect.api.refresh_access_token", fake_refresh
    )

    async def on_update(tokens):
        persisted.update(tokens)

    asyncio.run(_api(session, token_update_callback=on_update).get_printers())

    assert len(session.calls) == 2
    assert session.calls[1]["headers"]["Authorization"] == "Bearer NEW"
    assert persisted["access_token"] == "NEW"


def test_401_twice_raises_auth_failed(monkeypatch):
    """If the refreshed token is also rejected, reauth is required."""
    from homeassistant.exceptions import ConfigEntryAuthFailed

    session = _Session(_Response(status=401), _Response(status=401))

    async def fake_refresh(_session, _refresh_token):
        return {"access_token": "NEW", "refresh_token": "NEWREFRESH"}

    monkeypatch.setattr(
        "custom_components.prusa_connect.api.refresh_access_token", fake_refresh
    )

    with pytest.raises(ConfigEntryAuthFailed):
        asyncio.run(_api(session).get_printers())


def test_camera_snapshot_path():
    """Snapshots come from the camera's last stored frame."""
    session = _Session(_Response(body=b"\xff\xd8jpeg"))
    data = asyncio.run(_api(session).get_camera_snapshot(588016))

    assert session.calls[0]["url"] == (
        f"{API_BASE_URL}/app/cameras/588016/snapshots/last"
    )
    assert data == b"\xff\xd8jpeg"


def test_preview_url_is_fetched_verbatim(job):
    """The job's preview_url is already API-relative and includes /app."""
    session = _Session(_Response(body=b"\x89PNG"))
    url = job["file"]["preview_url"]
    data = asyncio.run(_api(session).get_bytes(url))

    assert session.calls[0]["url"] == f"{API_BASE_URL}{url}"
    assert data == b"\x89PNG"


def test_binary_fetch_returns_none_on_error():
    """A missing snapshot yields None rather than raising."""
    session = _Session(_Response(status=404))
    assert asyncio.run(_api(session).get_camera_snapshot(1)) is None
