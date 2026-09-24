"""The suggested-update review flow — `scqo/cli/_review.py`.

In-process and TTY-free: everything here is about what happens AROUND the
prompt, so `input` is never reached.

The load-bearing behaviour is the instrument release. A review blocks on a
human, and on a backend that holds its connection between acquisitions (Qblox
does; QM closes its QuantumMachine per acquisition) an operator who walks away
would pin the instrument for as long as they are gone. Nothing below the prompt
needs it — a decision writes the in-memory config and the JSON stores, and the
values reach hardware at the next run, which reconnects.
"""

from __future__ import annotations

import pytest

from scqo import Session
from scqo.cli._review import announce_suggestions, review_interactively
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)

ROWS = [{"entity": "q0", "field": "t1_s", "role": "fact",
         "before": None, "after": 4.1e-5, "status": "pending"}]


class _Session:
    """Just enough Session for the review flow up to the prompt."""

    def __init__(self, released=()):
        self.released = list(released)
        self.calls = 0

    def release_instruments(self):
        self.calls += 1
        return self.released

    def accept(self, run_id, **kwargs):
        raise AssertionError("the prompt was never answered; accept must not run")


@pytest.fixture()
def session(tmp_path):
    roster = demo_components()
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    return Session(
        SimulatedBackend(vendor), roster, design=design,
        scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
        device_name="chipT", backend_label="simulated",
        setup_name="sim", cooldown_id="cd1")


def _tty(monkeypatch, value: bool) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: value, raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: value, raising=False)


def test_the_instrument_is_released_before_the_prompt(monkeypatch, capsys):
    sess = _Session(released=["cluster0"])
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda: "n")  # decline immediately

    assert review_interactively(sess, "run-1", ROWS) is None
    assert sess.calls == 1

    err = capsys.readouterr().err
    assert "released cluster0 before the prompt" in err
    # released BEFORE the question, not after it was answered
    assert err.index("released cluster0") < err.index("nothing applied")


def test_a_backend_holding_nothing_says_nothing(monkeypatch, capsys):
    sess = _Session(released=[])
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda: "")

    review_interactively(sess, "run-1", ROWS)
    assert sess.calls == 1
    assert "released" not in capsys.readouterr().err


def test_nothing_is_released_when_there_is_no_prompt(monkeypatch, capsys):
    """A pipe or a script never blocks, so it never needs the connection back —
    and a later step in the same process may still want it."""
    sess = _Session(released=["cluster0"])
    _tty(monkeypatch, False)

    assert review_interactively(sess, "run-1", ROWS) is None
    assert sess.calls == 0
    err = capsys.readouterr().err
    assert "not a terminal" in err and "released" not in err


def test_an_empty_list_reaches_neither_the_release_nor_the_table(monkeypatch, capsys):
    sess = _Session(released=["cluster0"])
    _tty(monkeypatch, True)

    assert review_interactively(sess, "run-1", []) is None
    assert sess.calls == 0
    assert capsys.readouterr().err == ""


def test_announce_prints_the_table_and_one_trailer(capsys):
    announce_suggestions(ROWS, "decide later with: scqo accept run-1")
    err = capsys.readouterr().err
    assert "suggested updates (1 pending):" in err
    assert "t1_s" in err
    assert err.rstrip().endswith("decide later with: scqo accept run-1")

    announce_suggestions([], "unreachable")  # nothing pending, nothing printed
    assert capsys.readouterr().err == ""


def test_session_release_degrades_instead_of_failing(session, monkeypatch):
    """Losing the release must never cost the accept step — the same doctrine
    as the statistics figure. A backend without the hook is silent; one that
    raises warns and reports nothing released."""
    assert session.release_instruments() == []  # simulated: the ABC default

    def boom():
        raise RuntimeError("the transport loop is gone")

    monkeypatch.setattr(session.backend, "release_instruments", boom, raising=False)
    with pytest.warns(UserWarning, match="releasing the instrument failed"):
        assert session.release_instruments() == []

    monkeypatch.setattr(session.backend, "release_instruments",
                        lambda: ["cluster0"], raising=False)
    assert session.release_instruments() == ["cluster0"]
