"""One lab's QPU characterization report — an xlsx dashboard and a .pptx deck.

**This package is deliberately fenced off, and the fence is the point.** SCQO is
a vendor-neutral core shared by other labs and published under BSD-3-Clause;
what lives here is one lab's reporting convention — its sheet names, its column
order, its field dictionary, its Traditional-Chinese descriptions. Keeping that
inside the core is a decision the maintainer made knowingly, and it is only
tenable while the boundary holds:

* Every lab-specific string lives in ``field_dictionary.toml``. Another lab
  swaps that file and gets its own workbook; no Python changes. A convention
  that leaks back into :mod:`~scqo.viewer.lab_report.xlsx` or
  :mod:`~scqo.viewer.lab_report.slides` is the thing the split exists to stop.
* **Nothing in ``scqo/`` imports this package** except the one registration
  line in :mod:`scqo.viewer.app`. ``tests/test_lab_report_isolation.py``
  enforces that in both directions, so the day this moves lab-side it is a
  directory move and not a refactor.
* The builders are pure ``data -> bytes`` and import no fastapi, the same
  contract :mod:`scqo.viewer._export` states for the core exports.

The numbers themselves are NOT lab-specific and live in
:mod:`~scqo.viewer.lab_report.metrics`, which knows no sheet name and no
language.
"""

from __future__ import annotations

from .metrics import extract_chip_metrics
from .routes import register_lab_report_routes
from .slides import PPTX_MEDIA_TYPE, presentation_pptx_bytes
from .xlsx import XLSX_MEDIA_TYPE, lab_template_xlsx_bytes

__all__ = [
    "XLSX_MEDIA_TYPE", "PPTX_MEDIA_TYPE",
    "lab_template_xlsx_bytes", "presentation_pptx_bytes",
    "extract_chip_metrics", "register_lab_report_routes",
]
