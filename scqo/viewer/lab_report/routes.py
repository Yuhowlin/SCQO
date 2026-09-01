"""The four export routes, registered onto the viewer app from one call.

``app.py`` builds its handlers inside ``create_app``'s closure, so everything
these routes need is INJECTED rather than imported: the datastore, the two
context builders, and the Content-Disposition helper. That is what keeps this
package importable (and testable) without standing up a FastAPI app, and it is
the seam that lets the whole directory move lab-side later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import HTTPException, Query, Response

from .slides import PPTX_MEDIA_TYPE, presentation_pptx_bytes
from .sources import load_predicted_f_q
from .xlsx import XLSX_MEDIA_TYPE, lab_template_xlsx_bytes

#: the estimators campaign_query actually implements — typed here so FastAPI
#: rejects a misspelling instead of silently falling back to the mean.
Estimator = Literal["mean", "median"]

_XLSX_HINT = ("xlsx export needs openpyxl — reinstall the viewer extra: "
              "uv pip install -e <SCQO repo>[viewer]")
_PPTX_HINT = ("pptx export needs python-pptx — reinstall the viewer extra: "
              "uv pip install -e <SCQO repo>[viewer]")


def _tags(raw: str) -> list[str] | None:
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    return parts or None


def register_lab_report_routes(
    app: Any, *,
    store: Any,
    setup_context: Callable[[str, str, str], dict],
    unified_context: Callable[[str, str], dict],
    attachment: Callable[[str, str, str, str], dict],
) -> None:
    """Mount the lab-report exports on ``app``. Called once from ``create_app``."""

    data_root: Path = Path(store.data_root)

    def _build(ctx: dict, builder, hint: str, **kwargs) -> bytes:
        try:
            return builder(
                ctx, store=store, data_root=data_root,
                predicted_f_q=load_predicted_f_q(
                    data_root, ctx.get("device", ""), ctx.get("cooldown", ""),
                    ctx.get("setup_name", "")),
                **kwargs)
        except ImportError:
            raise HTTPException(503, hint)

    def _ctx(device: str, cooldown: str, setup_name: str, unified: bool) -> dict:
        return (unified_context(device, cooldown) if unified
                else setup_context(device, cooldown, setup_name))

    @app.get("/setup/{device}/{cooldown}/{setup_name}/export_dashboard.xlsx")
    def setup_export_dashboard_xlsx(
        device: str, cooldown: str, setup_name: str, unified: bool = False,
        min_repeats: int = Query(2, ge=1), estimator: Estimator = "mean",
        tags: str = "",
    ):
        ctx = _ctx(device, cooldown, setup_name, unified)
        data = _build(ctx, lab_template_xlsx_bytes, _XLSX_HINT,
                      min_repeats=min_repeats, estimator=estimator, tags=_tags(tags))
        name = "unified" if unified else setup_name
        return Response(data, media_type=XLSX_MEDIA_TYPE,
                        headers=attachment(device, cooldown, name, "dashboard.xlsx"))

    @app.get("/setup/{device}/{cooldown}/{setup_name}/export.pptx")
    def setup_export_pptx(
        device: str, cooldown: str, setup_name: str, presenter: str = "",
        design_name: str = "", goal: str = "", unified: bool = False,
        min_repeats: int = Query(2, ge=1), estimator: Estimator = "mean",
        tags: str = "",
    ):
        ctx = _ctx(device, cooldown, setup_name, unified)
        data = _build(ctx, presentation_pptx_bytes, _PPTX_HINT,
                      presenter=presenter, design_name=design_name, goal=goal,
                      min_repeats=min_repeats, estimator=estimator, tags=_tags(tags))
        name = "unified" if unified else setup_name
        return Response(data, media_type=PPTX_MEDIA_TYPE,
                        headers=attachment(device, cooldown, name, "pptx"))

    @app.get("/cooldown/{device}/{cooldown}/export_dashboard.xlsx")
    def cooldown_export_dashboard_xlsx(
        device: str, cooldown: str, min_repeats: int = Query(2, ge=1),
        estimator: Estimator = "mean", tags: str = "",
    ):
        data = _build(unified_context(device, cooldown), lab_template_xlsx_bytes,
                      _XLSX_HINT, min_repeats=min_repeats, estimator=estimator,
                      tags=_tags(tags))
        return Response(data, media_type=XLSX_MEDIA_TYPE,
                        headers=attachment(device, cooldown, "unified", "dashboard.xlsx"))

    @app.get("/cooldown/{device}/{cooldown}/export.pptx")
    def cooldown_export_pptx(
        device: str, cooldown: str, presenter: str = "", design_name: str = "",
        goal: str = "", min_repeats: int = Query(2, ge=1),
        estimator: Estimator = "mean", tags: str = "",
    ):
        data = _build(unified_context(device, cooldown), presentation_pptx_bytes,
                      _PPTX_HINT, presenter=presenter, design_name=design_name,
                      goal=goal, min_repeats=min_repeats, estimator=estimator,
                      tags=_tags(tags))
        return Response(data, media_type=PPTX_MEDIA_TYPE,
                        headers=attachment(device, cooldown, "unified", "pptx"))
