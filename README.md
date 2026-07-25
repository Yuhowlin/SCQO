# SCQO — Superconducting Qubit Orchestration

Instrument-agnostic orchestration layer for superconducting-qubit calibration experiments at the
level of experiment + parameters (`probe → run → estimate → update`). It is the vendor-neutral hub
shared by the Quantum Machines and Qblox driver repos, and the substrate for AI-driven experiment
loops (decide → run → estimate → extract → decide next).

- **Setting up a machine?** [INSTALL.md](INSTALL.md) — environment, lab config, offline
  tests, and the self-test against your real device config.
- **Measuring?** [TUTORIAL.md](TUTORIAL.md) — the student guide: run experiments, find
  your data, tag it, work from notebooks.
- **Architecture & conventions:** [CLAUDE.md](CLAUDE.md).
