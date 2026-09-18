import { expect, it } from "vitest";
import { firstRunText } from "./ApprovalDetail";

it("follows the run an approval queued", () => {
  expect(firstRunText({ state: "queued", queued_at: null })).toBe("Applying the changes now…");
  expect(
    firstRunText({ state: "done", queued_at: null, run_id: "r", status: "OK", trigger: "approval", rows_affected: 6 }),
  ).toMatch(/^Changes applied \(6 rows updated\)/);
  expect(
    firstRunText({ state: "done", queued_at: null, run_id: "r", status: "NOOP", trigger: "approval", rows_affected: 0 }),
  ).toMatch(/nothing needed changing/);
});

it("never claims a run that wasn't queued", () => {
  expect(firstRunText(null)).toBe("The changes apply on the sheet's next edit.");
});
