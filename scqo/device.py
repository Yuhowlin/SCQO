"""Device model — neutral names for qubit calibration state.

A protocol's ``update()`` writes fitted quantities back through these neutral names.
Each backend maps them onto its native model:

    neutral            QM / QUAM                     Qblox / QuantumDevice
    -----------------  ----------------------------  -----------------------------
    readout_freq       q.resonator.RF_frequency      q.clock_freqs.readout
    drive_freq         q.f_01 / q.xy.RF_frequency    q.clock_freqs.f01
    pi_amp             q.xy.operations['x180'].amp   q.rxy.amp180

This keeps experiment physics free of any vendor attribute path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class QubitView(ABC):
    """Backend-agnostic accessor for one qubit's calibration parameters.

    Concrete backends override each property to read/write their native device tree.
    Add more neutral fields here as experiments need them (T1, anharmonicity, ...).
    """

    name: str

    @property
    @abstractmethod
    def readout_freq(self) -> float:
        """Resonator readout frequency (Hz)."""

    @readout_freq.setter
    @abstractmethod
    def readout_freq(self, value: float) -> None: ...

    @property
    @abstractmethod
    def drive_freq(self) -> float:
        """Qubit 0->1 drive frequency (Hz)."""

    @drive_freq.setter
    @abstractmethod
    def drive_freq(self, value: float) -> None: ...

    @property
    @abstractmethod
    def pi_amp(self) -> float:
        """Amplitude of the calibrated pi (x180) pulse."""

    @pi_amp.setter
    @abstractmethod
    def pi_amp(self, value: float) -> None: ...


class DeviceModel(ABC):
    """Container of :class:`QubitView` objects plus persistence."""

    @abstractmethod
    def qubit(self, name: str) -> QubitView:
        """Return the view for a single qubit by name."""

    @abstractmethod
    def save(self) -> None:
        """Persist current device state (e.g. to JSON)."""

    @abstractmethod
    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of all qubit state (AI loop memory)."""
