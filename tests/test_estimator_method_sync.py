"""SCQO ``*_method`` Literals must equal the scqat registries they mirror.

The pydantic Literal is the AI-loop's decision surface; the scqat registry is
the implementation. Nothing else enforces they stay in sync — an unsynced
Literal either hides a new method or advertises a nonexistent one. One entry
per (experiment parameter, scqat registry) pair; extend when an experiment
gains a method axis.
"""

from typing import get_args

from scqo.experiments.resonator_spectroscopy import ResonatorSpectroscopyParameters
from scqo.experiments.resonator_spectroscopy_flux import ResonatorSpectroscopyFluxParameters
from scqo.experiments.resonator_spectroscopy_power_amp import ResonatorSpectroscopyPowerAmpParameters
from scqo.experiments.resonator_spectroscopy_power_chain import ResonatorSpectroscopyPowerChainParameters


def _literal_values(model: type, field: str) -> set:
    return set(get_args(model.model_fields[field].annotation))


def test_resonator_spectroscopy_analysis_method_matches_registry():
    from scqat.estimators.resonator_spectroscopy.methods import METHODS

    assert _literal_values(ResonatorSpectroscopyParameters, "analysis_method") == set(METHODS)


def test_resonator_flux_analysis_method_matches_registry():
    from scqat.estimators.resonator_spectroscopy_flux import METHODS

    assert _literal_values(ResonatorSpectroscopyFluxParameters, "analysis_method") == set(METHODS)


def test_resonator_flux_dip_method_matches_registry():
    from scqat.tools.dip_fit import DIP_METHODS

    assert _literal_values(ResonatorSpectroscopyFluxParameters, "dip_method") == set(DIP_METHODS)


def test_resonator_power_dip_method_matches_registry():
    from scqat.tools.dip_fit import DIP_METHODS

    for model in (ResonatorSpectroscopyPowerAmpParameters,
                  ResonatorSpectroscopyPowerChainParameters):
        assert _literal_values(model, "dip_method") == set(DIP_METHODS)
