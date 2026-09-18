import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import fixture from "../fixtures/describe.reference.json";
import type { Readback as ReadbackData } from "../api/types";
import { Readback } from "./Readback";

const data = fixture as ReadbackData;

afterEach(cleanup);

describe("Readback (reference config fixture = GET /configs/{id}/describe payload)", () => {
  it("renders the live-resolved header, stages and all three rules in order", () => {
    const { container, getByText } = render(<Readback data={data} />);
    getByText("Organizes 2 tabs with 3 rules — currently: MACHINES and SPARES.");
    const rules = [...container.querySelectorAll("[data-rule]")].map((li) => li.getAttribute("data-rule"));
    expect(rules).toEqual(["1", "2", "3"]);
    expect(container.textContent).toContain("in the order shown under Stages");
    expect(container.textContent).toContain("right after rule 1 runs");
    expect(container.textContent).toContain("Sorted the same way as rule 1.");
    expect(container.textContent).toContain("(overdue)");
  });

  it("shows colours as swatches with the hex only in the tooltip, never as bare text", () => {
    const { container } = render(<Readback data={data} />);
    const swatches = [...container.querySelectorAll("[data-hex]")];
    expect(swatches.length).toBeGreaterThan(0);
    const pink = swatches.find((s) => s.getAttribute("title") === "#FCE4EC");
    expect(pink?.textContent).toBe("pink");
    expect(container.textContent).not.toMatch(/#[0-9A-Fa-f]{6}/);
  });

  it("opens the format list with the starts-neutral / later-wins line", () => {
    const { container } = render(<Readback data={data} />);
    const format = container.querySelector('[data-rule="2"] ul li');
    expect(format?.textContent).toMatch(/^Each row starts as black text, no fill; the rules below/);
  });
});
