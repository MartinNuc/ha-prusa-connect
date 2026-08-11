"""OAuth2 PKCE authentication against Prusa Account."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
from urllib.parse import parse_qs, urlparse

import aiohttp

from .const import (
    AUTH_AUTHORIZE_URL,
    AUTH_CLIENT_ID,
    AUTH_REDIRECT_URI,
    AUTH_SCOPE,
    AUTH_TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

ACCOUNT_ORIGIN = "https://account.prusa3d.com"


class AuthenticationError(Exception):
    """Base authentication error."""


class InvalidCredentialsError(AuthenticationError):
    """Invalid email or password."""


class TotpRequiredError(AuthenticationError):
    """Two-factor authentication TOTP code is required."""

    def __init__(self, session_data: dict) -> None:
        """Initialize with session data needed to continue auth."""
        super().__init__("TOTP code required")
        self.session_data = session_data


class TotpInvalidError(AuthenticationError):
    """Invalid TOTP code."""


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _extract_csrf_token(html: str) -> str | None:
    """Extract the CSRF token from a Prusa Account form."""
    match = re.search(
        r'<input[^>]+name=["\']csrfmiddlewaretoken["\'][^>]+value=["\']([^"\']+)',
        html,
    )
    return match.group(1) if match else None


def _find_otp_field(html: str) -> str | None:
    """Return the name of the one-time-code input on a page, if present.

    Prusa currently calls it ``otp_token``, but the name is not documented, so
    match anything that looks like a one-time code. ``csrfmiddlewaretoken`` is
    skipped because its name also contains "token".
    """
    for name in re.findall(r'<input[^>]+name=["\']([^"\']+)', html):
        lowered = name.lower()
        if lowered == "csrfmiddlewaretoken":
            continue
        if "otp" in lowered or "2fa" in lowered or "two_factor" in lowered:
            return name
    return None


def _is_totp_page(url: str, html: str) -> bool:
    """Detect the two-factor form.

    Matches the OTP input field or the URL path (Prusa uses /login/totp/),
    rather than the word "totp" appearing anywhere in a large marketing page.
    """
    path = urlparse(url).path.lower()
    if any(token in path for token in ("otp", "two-factor", "2fa", "verify")):
        return True
    return _find_otp_field(html) is not None


def decode_id_token(id_token: str) -> dict:
    """Decode the (already server-validated) id_token payload.

    Connect has no user endpoint, so account identity comes from this claim set.
    The token is only used for its user id here — it arrives over TLS directly
    from the token endpoint, so it is not re-verified locally.
    """
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError) as err:
        raise AuthenticationError(f"Could not decode id_token: {err}") from err


async def authenticate(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
) -> dict:
    """Authenticate with Prusa Account and return tokens.

    Raises TotpRequiredError if 2FA is enabled, InvalidCredentialsError if the
    credentials are rejected.
    """
    code_verifier, code_challenge = _generate_pkce()

    authorize_params = {
        "response_type": "code",
        "client_id": AUTH_CLIENT_ID,
        "redirect_uri": AUTH_REDIRECT_URI,
        "scope": AUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    # A cookie jar keeps the Django session across the login redirects.
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as auth_session:
        async with auth_session.get(
            AUTH_AUTHORIZE_URL,
            params=authorize_params,
            allow_redirects=True,
        ) as resp:
            login_html = await resp.text()
            # Keeps ?next=/o/authorize/... so login resumes the OAuth flow.
            login_url = str(resp.url)

        csrf_token = _extract_csrf_token(login_html)
        if not csrf_token:
            raise AuthenticationError("Could not find CSRF token in login page")

        login_data = {
            "csrfmiddlewaretoken": csrf_token,
            "email": email,
            "password": password,
        }

        async with auth_session.post(
            login_url,
            data=login_data,
            headers={"Referer": login_url},
            allow_redirects=False,
        ) as resp:
            if resp.status == 200:
                # Re-rendered the form: bad credentials, or a 2FA challenge
                # served in place.
                response_html = await resp.text()
                if _is_totp_page(str(resp.url), response_html):
                    raise TotpRequiredError(
                        _totp_session(
                            code_verifier, str(resp.url), response_html, jar
                        )
                    )
                raise InvalidCredentialsError("Invalid email or password")

            location = resp.headers.get("Location", "")

        code, final_url, final_html = await _follow_redirects_for_code(
            auth_session, location
        )

        if code:
            return await _exchange_code(session, code, code_verifier)

        # With 2FA enabled the password POST redirects to /login/totp/ instead
        # of continuing to the callback, so the chain ends on the challenge
        # page rather than yielding a code.
        if final_html is not None and _is_totp_page(final_url or "", final_html):
            raise TotpRequiredError(
                _totp_session(code_verifier, final_url or "", final_html, jar)
            )

        raise AuthenticationError("Could not obtain authorization code")


def _totp_session(
    code_verifier: str, url: str, html: str, jar: aiohttp.CookieJar
) -> dict:
    """Build the state needed to continue authentication after the 2FA form."""
    return {
        "code_verifier": code_verifier,
        "totp_url": url,
        "totp_csrf": _extract_csrf_token(html),
        "totp_field": _find_otp_field(html) or "otp_token",
        "jar": jar,
    }


async def authenticate_totp(
    session: aiohttp.ClientSession,
    totp_code: str,
    session_data: dict,
) -> dict:
    """Complete authentication with a TOTP code."""
    jar = session_data["jar"]
    code_verifier = session_data["code_verifier"]
    field = session_data.get("totp_field") or "otp_token"
    csrf = session_data.get("totp_csrf")

    if not csrf:
        raise AuthenticationError("Could not find CSRF token on the 2FA form")

    async with aiohttp.ClientSession(cookie_jar=jar) as auth_session:
        # The challenge URL carries ?next=/o/authorize/..., so posting back to
        # it resumes the OAuth flow without re-sending the `next` field.
        totp_data = {"csrfmiddlewaretoken": csrf, field: totp_code}

        async with auth_session.post(
            session_data["totp_url"],
            data=totp_data,
            headers={"Referer": session_data["totp_url"]},
            allow_redirects=False,
        ) as resp:
            if resp.status == 200:
                raise TotpInvalidError("Invalid TOTP code")

            location = resp.headers.get("Location", "")

        code, _final_url, _final_html = await _follow_redirects_for_code(
            auth_session, location
        )

        if not code:
            raise AuthenticationError(
                "Could not obtain authorization code after TOTP"
            )

        return await _exchange_code(session, code, code_verifier)


async def refresh_access_token(
    session: aiohttp.ClientSession,
    refresh_token: str,
) -> dict:
    """Refresh an access token using the refresh token."""
    data = {
        "grant_type": "refresh_token",
        "client_id": AUTH_CLIENT_ID,
        "refresh_token": refresh_token,
    }

    async with session.post(AUTH_TOKEN_URL, data=data) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            _LOGGER.error("Token refresh failed (%s): %s", resp.status, error_text)
            raise AuthenticationError(f"Token refresh failed: {resp.status}")

        token_data = await resp.json()
        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", refresh_token),
        }


async def _follow_redirects_for_code(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[str | None, str | None, str | None]:
    """Follow the redirect chain looking for the authorization code.

    Returns (code, final_url, final_html). The final page is handed back so the
    caller can tell a 2FA challenge apart from a genuine failure.
    """
    max_redirects = 10
    for _ in range(max_redirects):
        if not url:
            return None, None, None

        parsed = urlparse(url)
        if "code" in parse_qs(parsed.query):
            return parse_qs(parsed.query)["code"][0], url, None

        if not parsed.scheme:
            url = f"{ACCOUNT_ORIGIN}{url}"

        async with session.get(url, allow_redirects=False) as resp:
            if resp.status not in (301, 302, 303, 307, 308):
                # Landed on a real page — return it for inspection.
                return None, str(resp.url), await resp.text()

            url = resp.headers.get("Location", "")
            redirect_qs = parse_qs(urlparse(url).query)
            if "code" in redirect_qs:
                return redirect_qs["code"][0], url, None

    return None, url, None


async def _exchange_code(
    session: aiohttp.ClientSession,
    code: str,
    code_verifier: str,
) -> dict:
    """Exchange the authorization code for tokens."""
    data = {
        "grant_type": "authorization_code",
        "client_id": AUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": AUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
    }

    async with session.post(AUTH_TOKEN_URL, data=data) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            raise AuthenticationError(
                f"Token exchange failed ({resp.status}): {error_text}"
            )

        token_data = await resp.json()
        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "id_token": token_data.get("id_token", ""),
        }
