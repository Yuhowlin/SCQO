"""The lab template, read from ``field_dictionary.toml`` — the ONE place a lab
convention enters this package.

Loaded through ``importlib.resources`` so it works from an installed wheel and
not only from a source checkout. Cached: the file is small and immutable for
the process's life.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py3.10
    import tomli as tomllib  # type: ignore[no-redef]

#: the data file inside this package
DATA_FILE = "field_dictionary.toml"


@dataclass(frozen=True)
class DictionaryRow:
    """One row of the field-dictionary sheet, verbatim from the TOML."""

    section: str
    item: float
    name: str
    entry: str
    unit: str
    doc: str
    tier: str

    def as_cells(self) -> list[Any]:
        return [self.section, self.item, self.name, self.entry, self.unit,
                self.doc, self.tier]


@dataclass(frozen=True)
class Template:
    sheets: dict[str, str]
    dictionary_header: list[str]
    dictionary: tuple[DictionaryRow, ...]

    def sheet(self, key: str, default: str = "") -> str:
        return self.sheets.get(key, default)


@lru_cache(maxsize=1)
def load_template() -> Template:
    """The packaged template. Raises if the data file is missing — a report
    with no template is not a degraded report, it is a broken install."""
    text = resources.files(__package__).joinpath(DATA_FILE).read_text(encoding="utf-8")
    data = tomllib.loads(text)
    sheets = dict(data.get("sheets") or {})
    rows = tuple(
        DictionaryRow(
            section=str(r.get("section", "")), item=float(r.get("item", 0.0)),
            name=str(r.get("name", "")), entry=str(r.get("entry", "")),
            unit=str(r.get("unit", "")), doc=str(r.get("doc", "")),
            tier=str(r.get("tier", "")),
        )
        for r in (data.get("dictionary") or [])
    )
    if not rows:
        raise ValueError(f"{DATA_FILE} declares no dictionary rows")
    return Template(
        sheets=sheets,
        dictionary_header=list(data.get("dictionary_header") or []),
        dictionary=rows,
    )
