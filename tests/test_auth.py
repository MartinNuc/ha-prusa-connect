"""Authentication helpers, including the two-factor challenge.

Prusa serves the 2FA form by redirecting to /login/totp/ rather than
re-rendering the login page, which the original code treated as a failure.
"""

from __future__ import annotations

import base64
import json

import pytest

from custom_components.prusa_connect.auth import (
    AuthenticationError,
    _extract_csrf_token,
    _find_otp_field,
    _is_totp_page,
    _totp_session,
    decode_id_token,
)

TOTP_URL = (
    "https://account.prusa3d.com/login/totp/?next=/o/authorize/%3Fresponse_type%3Dcode"
)
TOTP_HTML = (
    '<form method="post" action="?next=/o/authorize/">'
    '<input type="hidden" name="csrfmiddlewaretoken" value="CSRF123">'
    '<input type="hidden" name="next" value="/o/authorize/">'
    '<input type="text" name="otp_token" autofocus required>'
    "</form>"
)

LOGIN_URL = "https://account.prusa3d.com/login/?next=/o/authorize/"
LOGIN_HTML = (
    '<form id="recaptcha_form" method="post">'
    '<input type="hidden" name="csrfmiddlewaretoken" value="CSRF123">'
    '<input type="hidden" name="next" value="/o/authorize/">'
    '<input type="text" name="email">'
    '<input type="password" name="password">'
    "</form>"
)


def test_totp_page_is_detected():
    """The real challenge page is recognised."""
    assert _is_totp_page(TOTP_URL, TOTP_HTML) is True


def test_login_page_is_not_mistaken_for_totp():
    """Regression: matching the substring "totp" anywhere flagged the login page."""
    assert _is_totp_page(LOGIN_URL, LOGIN_HTML) is False


def test_totp_detected_by_url_when_markup_is_unfamiliar():
    """Detection still works if the form markup changes."""
    assert _is_totp_page(TOTP_URL, "<html>redesigned</html>") is True


def test_otp_field_is_discovered():
    """The one-time-code field name is read from the form."""
    assert _find_otp_field(TOTP_HTML) == "otp_token"


def test_csrf_is_not_mistaken_for_the_otp_field():
    """csrfmiddlewaretoken also contains "token" and must be skipped."""
    assert _find_otp_field(LOGIN_HTML) is None


def test_renamed_otp_field_still_found():
    """A different spelling of the field is tolerated."""
    html = (
        '<input name="csrfmiddlewaretoken" value="X">'
        '<input name="two_factor_code">'
    )
    assert _find_otp_field(html) == "two_factor_code"


def test_csrf_extraction():
    """The CSRF token is pulled from the form."""
    assert _extract_csrf_token(TOTP_HTML) == "CSRF123"


def test_totp_session_carries_what_the_second_step_needs():
    """The challenge state survives to the TOTP step of the config flow."""
    session = _totp_session("VERIFIER", TOTP_URL, TOTP_HTML, jar=None)

    assert session["code_verifier"] == "VERIFIER"
    assert session["totp_url"] == TOTP_URL
    assert session["totp_csrf"] == "CSRF123"
    assert session["totp_field"] == "otp_token"


def _id_token(payload: dict) -> str:
    """Build an unsigned JWT with the given payload."""
    encode = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return ".".join(
        [
            encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()),
            encode(json.dumps(payload).encode()),
            "signature",
        ]
    )


def test_id_token_yields_account_identity():
    """Connect has no user endpoint, so identity comes from the id_token."""
    token = _id_token({"user": {"id": 2781634, "email": "user@example.com"}})
    claims = decode_id_token(token)

    assert claims["user"]["id"] == 2781634
    assert claims["user"]["email"] == "user@example.com"


def test_id_token_padding_is_handled():
    """base64url payloads are decoded regardless of padding length."""
    for name in ("a", "ab", "abc", "abcd"):
        token = _id_token({"user": {"id": 1, "email": f"{name}@example.com"}})
        assert decode_id_token(token)["user"]["email"] == f"{name}@example.com"


def test_malformed_id_token_raises_authentication_error():
    """A corrupt token fails as an auth error, not an opaque crash."""
    with pytest.raises(AuthenticationError):
        decode_id_token("not-a-jwt")
