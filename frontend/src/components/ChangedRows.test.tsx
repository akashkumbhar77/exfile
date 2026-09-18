import { afterEach, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import type { PreviewTab } from "../api/types";
import { ChangedRows } from "./ChangedRows";

afterEach(cleanup);

const cell = (v: string, font: string | null = null, bg: string | null = null, strike = false) => ({ v, font, bg, strike });

const tab: PreviewTab = {
  tab: "MACHINES", exists: true, rows_to_reorder: 2, rows_to_recolor: 1, rows_added: 0, rows_removed: 0,
  rows_cleared: 0, cells_to_write: 4, cells_to_recolor: 2, changed_rows_total: 3,
  headers: ["STATUS", "NAME"],
  sample: [
    { row: 3, kind: "data", before: [cell("Completed"), cell("A")], after: [cell("In Process", "#FF0000"), cell("A")], changed: [0] },
    { row: 1, kind: "title", before: [cell(""), cell("")], after: [cell("Q3", "#FFFFFF", "#38761D"), cell("")], changed: [0] },
  ],
};

it("highlights only the changed cells, labels title rows, and notes truncation", () => {
  const { container, getByText } = render(<ChangedRows tab={tab} />);
  const highlighted = container.querySelectorAll("td.changed");
  expect(highlighted.length).toBe(4); // one changed cell per row, shown in both before and after
  getByText("title");
  getByText("Showing 2 of 3 changed rows.");
  const after = [...container.querySelectorAll("td")].find((td) => td.textContent === "In Process") as HTMLElement;
  expect(after.style.color).toBe("rgb(255, 0, 0)");
});
