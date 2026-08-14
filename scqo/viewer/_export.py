"""Setup-page export builders — pure ``rows -> bytes``, renderer-free (no fastapi).

Both builders consume the setup page's row dicts
``{entity, field, value, unit, source, previous}`` (:func:`app._param_rows`)
and ignore ``previous`` — an export carries current state only. The xlsx
builder lazy-imports openpyxl (the viewer extra); its ImportError propagates so
the route can answer with the reinstall hint. The PDF builder lazy-imports
headless matplotlib — guaranteed transitively via scqat — per the
``cli/_campaign_plot`` discipline.
"""

from __future__ import annotations

import io

from ..provenance import SOURCE_STATUSES

#: one naming authority for the xlsx route's response header
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: the two setup-page sections, in page order — shared by xlsx sheets and pdf pages
SECTIONS = ("Calibration", "Physical parameters")

_HEADER = ("entity", "parameter", "value", "unit", "source")


def _fmt(v) -> str:
    """The page's value formatting (setup.html): %.8g for numerics, str otherwise."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:.8g}"
    return str(v)


def source_label(src: dict | None) -> str:
    """One short filterable string per provenance status (the xlsx source cell)."""
    if not src:
        return "unrecorded"
    status = src.get("status")
    if status == "run":
        return f"run:{src.get('run_id')}"
    if status == "campaign":
        return f"campaign:{src.get('campaign_id')}"
    return status or "unrecorded"


def _source_detail(src: dict | None) -> str:
    """The pdf per-row source column: which run/campaign/operator, or the
    drifted value for an external row."""
    if not src:
        return "-"
    status = src.get("status")
    if status == "run":
        return src.get("run_id") or "-"
    if status == "campaign":
        return src.get("campaign_id") or "-"
    if status == "manual":
        return src.get("operator") or "manual"
    if status == "external":
        rec = src.get("recorded")
        return f"recorded {_fmt(rec)}" if rec is not None else "drifted"
    return "-"


def xlsx_bytes(state_rows: list[dict], physical_rows: list[dict]) -> bytes:
    """Two-sheet workbook mirroring the page sections, 5 columns each."""
    import openpyxl  # lazy: only the xlsx route pays for it (and can 503 without it)
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    for title, rows in zip(SECTIONS, (state_rows, physical_rows)):
        ws = wb.active if title == SECTIONS[0] else wb.create_sheet()
        ws.title = title
        ws.append(_HEADER)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            v = r["value"]
            if not isinstance(v, (int, float, str, bool)) and v is not None:
                v = str(v)  # list-shaped values exist; keep numerics native
            ws.append((r["entity"], r["field"], v, r["unit"] or "",
                       source_label(r["source"])))
        for col, width in zip("ABCDE", (16, 28, 18, 10, 44)):
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _kind_of(row: dict) -> str:
    src = row.get("source")
    return (src.get("status") or "unrecorded") if src else "unrecorded"


def pdf_bytes(state_rows: list[dict], physical_rows: list[dict], *,
              device: str, cooldown: str, setup_name: str,
              rows_per_page: int = 18) -> bytes:
    """16:9 landscape slides: per section (Calibration, then Physical
    parameters), one page per source kind in SOURCE_STATUSES order, rows
    sorted by entity, "(cont.)" pagination past ``rows_per_page``."""
    import matplotlib

    matplotlib.use("Agg")  # headless — the _campaign_plot discipline
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    context = f"{device} · {cooldown}/{setup_name}"
    pages: list[tuple[str, list[dict]]] = []
    for section, rows in zip(SECTIONS, (state_rows, physical_rows)):
        by_kind: dict[str, list[dict]] = {}
        for r in rows:
            by_kind.setdefault(_kind_of(r), []).append(r)
        for kind in SOURCE_STATUSES:
            # stable sort by entity keeps the catalog field order within an entity
            chunked = sorted(by_kind.get(kind, ()), key=lambda r: r["entity"])
            for start in range(0, len(chunked), rows_per_page):
                cont = " (cont.)" if start else ""
                pages.append((f"{section} — {kind}{cont}",
                              chunked[start:start + rows_per_page]))

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        if not pages:
            fig = plt.figure(figsize=(13.333, 7.5))
            fig.text(0.5, 0.55, f"Setup {context}", ha="center", fontsize=22)
            fig.text(0.5, 0.44, "No parameters recorded for this context.",
                     ha="center", fontsize=14, color="#6b7683")
            pdf.savefig(fig)
            plt.close(fig)
        for i, (title, rows) in enumerate(pages):
            fig = plt.figure(figsize=(13.333, 7.5))  # 16:9 slide
            fig.text(0.04, 0.90, title, fontsize=22, fontweight="bold")
            fig.text(0.04, 0.845, context, fontsize=13, color="#6b7683")
            fig.text(0.96, 0.03, f"{i + 1} / {len(pages)}",
                     ha="right", fontsize=10, color="#6b7683")
            fig.text(0.04, 0.03, "SCQO setup export",
                     fontsize=10, color="#6b7683")
            ax = fig.add_axes([0.04, 0.08, 0.92, 0.72])
            ax.axis("off")
            cells = [(r["entity"], r["field"], _fmt(r["value"]),
                      r["unit"] or "", _source_detail(r["source"]))
                     for r in rows]
            # constant row height across pages: the table's bbox height tracks
            # the row count (a 2-row page must not stretch to slide height)
            h = (len(cells) + 1) / (rows_per_page + 1)
            tbl = ax.table(cellText=cells, colLabels=_HEADER,
                           colWidths=[0.13, 0.25, 0.17, 0.08, 0.37],
                           cellLoc="left", bbox=[0.0, 1.0 - h, 1.0, h])
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(10)
            for (row, _col), cell in tbl.get_celld().items():
                cell.set_edgecolor("#e2e8ef")
                cell.set_linewidth(0.6)
                cell.PAD = 0.02
                if row == 0:  # header row (colLabels)
                    cell.set_facecolor("#f2f6fa")
                    cell.set_text_props(fontweight="bold")
            pdf.savefig(fig)
            plt.close(fig)
    return buf.getvalue()
