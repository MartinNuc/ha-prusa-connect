"""Button availability, checked against the printer's advertised command set."""

from __future__ import annotations

import pytest

from custom_components.prusa_connect.button import BUTTON_DESCRIPTIONS
from custom_components.prusa_connect.const import CMD_STATES


def _allowed(command: str, state: str) -> bool:
    """Whether a command may be issued from a given printer state."""
    return state in CMD_STATES.get(command, frozenset())


def test_command_names_are_advertised_by_the_printer(supported_commands):
    """Every button maps to a command the API actually accepts."""
    known = {c["command"] for c in supported_commands}
    for description in BUTTON_DESCRIPTIONS:
        assert description.command in known


def test_command_states_match_the_api(supported_commands):
    """Availability windows come from the API, not from guesswork."""
    api_states = {
        c["command"]: set(c["executable_from_state"]) for c in supported_commands
    }
    for command, states in CMD_STATES.items():
        assert set(states) == api_states[command]


@pytest.mark.parametrize(
    ("key", "state", "expected"),
    [
        ("pause_print", "PRINTING", True),
        ("pause_print", "IDLE", False),
        ("resume_print", "PAUSED", True),
        ("resume_print", "PRINTING", False),
        ("stop_print", "PRINTING", True),
        ("stop_print", "PAUSED", True),
        ("stop_print", "ATTENTION", True),
        ("stop_print", "IDLE", False),
        ("set_ready", "IDLE", True),
        ("set_ready", "FINISHED", True),
        ("set_ready", "PRINTING", False),
        ("cancel_ready", "READY", True),
        ("cancel_ready", "IDLE", False),
    ],
)
def test_availability_per_state(key, state, expected):
    """Buttons are offered only where the command is valid."""
    description = next(d for d in BUTTON_DESCRIPTIONS if d.key == key)
    assert _allowed(description.command, state) is expected


def test_idle_printer_offers_only_set_ready(printer):
    """The captured printer is IDLE, so only Set Ready applies."""
    state = printer["printer_state"]
    offered = {
        d.key for d in BUTTON_DESCRIPTIONS if _allowed(d.command, state)
    }
    assert offered == {"set_ready"}
