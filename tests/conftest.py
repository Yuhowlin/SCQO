"""Suite-wide test setup."""

import os

import pytest

# Headless, deterministic figure generation. Without this, matplotlib may pick the
# interactive TkAgg backend on Windows, and Tk initialization intermittently fails
# mid-suite (TclError: "Can't find a usable tk.tcl") — the artifact fallback in
# scqo/_scqat.py then drops the figure PNGs and layout tests flake.
os.environ.setdefault("MPLBACKEND", "Agg")


@pytest.fixture(autouse=True)
def _isolate_personal_scqo_files(monkeypatch, tmp_path):
    """No test may read the runner's real ~/.scqo files (config/parameters/user.toml).

    Found on the lab server: any account with a personal user.toml turned
    test_cli_backends red — every IN-PROCESS labconfig.load() is affected, not just
    test_labconfig (which additionally re-points these paths per test). Subprocess
    tests are already hermetic via SCQO_CONFIG / SCQO_USER_CONFIG in their env dicts.
    """
    from scqo import labconfig

    monkeypatch.setattr(labconfig, "DEFAULT_PATH", tmp_path / "no-config.toml")
    monkeypatch.setattr(labconfig, "PARAMS_DEFAULT_PATH", tmp_path / "no-parameters.toml")
    monkeypatch.setattr(labconfig, "USER_DEFAULT_PATH", tmp_path / "no-user.toml")
    monkeypatch.delenv(labconfig.ENV_VAR, raising=False)
    monkeypatch.delenv(labconfig.USER_ENV_VAR, raising=False)


class FakeClock:
    """A virtual clock for ``Session.run_campaign``'s ``monotonic``/``sleep`` seam.

    Campaign timing tests used to either really wait or monkeypatch the stdlib
    ``time`` module process-wide, and both produced failures that were about the
    machine rather than the code: a repeat finishing slower than a 0.4 s period
    (v3.6.0), and a 28-femtosecond shortfall against an exact 0.5 s threshold
    (v3.8.0). Virtual time removes the whole class — assertions become exact and
    the tests stop measuring the runner.

    ``sleep`` ADVANCES the clock, which is what lets the cadence gate resolve
    without waiting, and records the request so a test can assert that what was
    announced is what was slept. ``advance`` is the manual hook: driving it from
    an ``on_progress`` callback makes a repeat consume virtual time, which is the
    only way to reach the overrun branch (``overran_by_s``) that a real clock
    could reach only by genuinely running slow.

    The base is deliberately non-zero and not round, so a test that accidentally
    asserts against an absolute reading rather than an interval fails loudly
    instead of passing on a coincidence.
    """

    def __init__(self, start: float = 1_234.5, sleep_raises: BaseException | None = None):
        self.t = float(start)
        self.slept: list[float] = []
        self._sleep_raises = sleep_raises

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._sleep_raises is not None:
            raise self._sleep_raises
        self.t += seconds

    def advance(self, seconds: float) -> None:
        """Move virtual time forward without a sleep — work taking time."""
        self.t += seconds

    def ticker(self, per_step: float):
        """An ``on_progress`` callback that ages the clock on every step.

        Returns a callable that records events AND advances, so a test can make
        each step cost ``per_step`` virtual seconds. Read the events off
        ``.events``.
        """
        events: list[dict] = []

        def on_progress(event: dict):
            events.append(event)
            if event["kind"] == "step_done":
                self.advance(per_step)
            return None

        on_progress.events = events  # type: ignore[attr-defined]
        return on_progress
