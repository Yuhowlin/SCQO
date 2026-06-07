# SCQO — Superconducting Qubit Orchestration

Instrument-agnostic orchestration layer for superconducting-qubit calibration experiments at the
level of protocol + parameters (`build → run → analyze → update`). It is the vendor-neutral hub
shared by the Quantum Machines and Qblox driver repos, and the substrate for AI-driven experiment
loops (decide → run → analyze → extract → decide next).

See [CLAUDE.md](CLAUDE.md) for the full architecture, conventions, and operating rules.
