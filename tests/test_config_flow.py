"""What the config entry gets called, and why it matters.

The title is not merely cosmetic. Home Assistant reuses it verbatim in the one
place that identifies nothing else about us: when a token expires, core raises a
repair reading "Authentication expired for {title}", filed under Home Assistant
Core rather than this integration and — for a custom integration — with no brand
icon. A bare email address there tells the user an account expired without
saying which service it belongs to.
"""

from __future__ import annotations

import base64
import json

from custom_components.prusa_connect.config_flow import PrusaConnectConfigFlow

USER_ID = "468515"


def id_token(claims: dict) -> str:
    """Build an id_token carrying these claims.

    Only the payload segment is read — it arrives over TLS straight from the
    token endpoint and is not re-verified locally.
    """
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def account(claims: dict, email: str | None = None) -> tuple[str, str]:
    flow = PrusaConnectConfigFlow()
    flow._email = email
    return flow._account_from_tokens({"id_token": id_token(claims)})


class TestEntryTitle:
    """The title has to survive being quoted with no other context."""

    def test_names_the_service_as_well_as_the_account(self) -> None:
        _, label = account({"user": {"id": USER_ID, "email": "martin@nuc.cz"}})
        assert label == "Prusa Connect (martin@nuc.cz)"

    def test_says_which_service_even_with_no_account(self) -> None:
        """The account is unknown; "Authentication expired for" still needs a subject."""
        _, label = account({"sub": USER_ID})
        assert label == "Prusa Connect"

    def test_falls_back_to_the_email_that_was_typed_in(self) -> None:
        _, label = account({"sub": USER_ID}, email="typed@example.com")
        assert label == "Prusa Connect (typed@example.com)"

    def test_user_id_comes_from_the_claims(self) -> None:
        user_id, _ = account({"user": {"id": USER_ID, "email": "a@b.c"}})
        assert user_id == USER_ID

    def test_user_id_falls_back_to_sub(self) -> None:
        user_id, _ = account({"sub": "from-sub"})
        assert user_id == "from-sub"
