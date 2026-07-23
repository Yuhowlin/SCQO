"""Shared helpers for the offline experiment simulators."""

from __future__ import annotations

import hashlib

import numpy as np


def iq_from_population(
    population: np.ndarray,
    rng: np.random.Generator,
    *,
    sep_range: tuple[float, float] = (2.0, 4.0),
    noise: float = 0.02,
) -> tuple[np.ndarray, np.ndarray]:
    """Place a ``[0, 1]`` population trace in the IQ plane like a real averaged readout.

    The two states sit at a random ground offset ``pos0`` and ``pos0 + sep*e^{i*theta}``
    for a random rotation ``theta``; the averaged IQ point is the population-weighted
    mix ``pos0 + P*(pos1 - pos0)`` plus Gaussian noise in **both** quadratures. Returns
    ``(I, Q)`` rows. This is what makes the coherent-drive estimators actually exercise
    the IQ->1-D reduction — a simulator that put the signal in I and noise in Q would
    hide the ``rename(I->signal)`` bug the reduction fixes.
    """
    population = np.asarray(population, dtype=float)
    theta = float(rng.uniform(-np.pi, np.pi))
    sep = float(rng.uniform(*sep_range))
    pos0 = complex(float(rng.uniform(-1.0, 1.0)), float(rng.uniform(-1.0, 1.0)))
    z = pos0 + population * (sep * np.exp(1j * theta))
    z = z + rng.normal(0.0, noise, population.size) + 1j * rng.normal(0.0, noise, population.size)
    return np.real(z), np.imag(z)


def stable_seed(*parts: str) -> int:
    """A process-stable RNG seed derived from string parts.

    Unlike :func:`hash`, this does not depend on ``PYTHONHASHSEED``, so a simulator
    seeded with the same experiment + qubit names reproduces the same synthetic data
    across processes — which offline tests and AI dry-runs rely on.
    """
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")
