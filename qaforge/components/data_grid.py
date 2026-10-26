"""Reusable data grid / HTML table component - the most common widget in banking back-offices."""
from __future__ import annotations

from playwright.sync_api import Locator

from qaforge.core.base_page import BaseComponent


class DataGrid(BaseComponent):
    """Works with plain <table> grids out of the box. For div-based grids (AG Grid, MUI, Kendo)
    subclass and override the three selector attributes."""

    header_cells = "thead th"
    body_rows = "tbody tr"
    cells = "td"

    def headers(self) -> list[str]:
        # text_content, not inner_text: CSS text-transform (e.g. uppercase headers) must not change keys
        return [" ".join(h.split()) for h in self.root.locator(self.header_cells).all_text_contents()]

    def rows(self) -> list[dict[str, str]]:
        headers = self.headers()
        result = []
        for row in self.root.locator(self.body_rows).all():
            values = [c.strip() for c in row.locator(self.cells).all_inner_texts()]
            result.append(dict(zip(headers, values)))
        return result

    def row_where(self, **criteria: str) -> Locator:
        """Locator of the first row whose cells contain all criteria values (by column header)."""
        headers = self.headers()
        row = self.root.locator(self.body_rows)
        for column, value in criteria.items():
            col_key = column.replace("_", " ").lower()
            idx = next((i for i, h in enumerate(headers) if h.lower() == col_key), None)
            if idx is None:
                raise KeyError(f"Column '{column}' not in grid '{self.name}'. Columns: {headers}")
            row = row.filter(has=self.root.page.locator(f"{self.cells}:nth-child({idx + 1})", has_text=value))
        return row.first

    def read_row(self, **criteria: str) -> dict[str, str]:
        target = self.row_where(**criteria)
        self.act.expect_visible(target, f"{self.name} row {criteria}")
        values = [c.strip() for c in target.locator(self.cells).all_inner_texts()]
        return dict(zip(self.headers(), values))

    def row_count(self) -> int:
        return self.root.locator(self.body_rows).count()
