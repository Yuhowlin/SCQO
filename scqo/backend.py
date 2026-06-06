"""Backend — bridges an abstract protocol to a concrete instrument (or a simulator).

The backend owns the device model and knows how to *acquire* data for a protocol.
This is the only seam where vendor APIs (qm-qua, qblox-scheduler) appear.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import xarray as xr

from .device import DeviceModel

if TYPE_CHECKING:
    from .protocol import Protocol


class Backend(ABC):
    """An instrument adapter."""

    @property
    @abstractmethod
    def device(self) -> DeviceModel:
        """The device model whose state experiments read and update."""

    @abstractmethod
    def acquire(self, protocol: "Protocol") -> xr.Dataset:
        """Realize and execute ``protocol`` on this backend, returning labelled data.

        Hardware backends call ``protocol.build()`` to produce a native program
        (a QUA program or a Qblox ``Schedule``), run it, and return the result as an
        ``xarray.Dataset`` with a ``qubit`` dimension plus the protocol's sweep axes.
        The simulated backend ignores ``build`` and calls ``protocol.simulate`` instead.
        """
