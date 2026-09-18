import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { setToken } from "../../api/client";
import type { Job } from "../../api/types";
import { EnrollPage } from "./EnrollPage";

const SA = "sheets-bot@example-project.iam.gserviceaccount.com";
let calls: Array<{ url: string; method: string; body: unknown }> = [];
let jobStates: Job[] = [];
let accessReply: unknown = null;

const job = (over: Partial<Job>): Job => ({
  job_id: "j1", state: "queued", sheet_id: 7, config_id: null, config_version: null,
  failure: null, failures: [], done: false, ...over,
});

beforeEach(() => {
  calls = [];
  setToken("t0k");
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    const method = init.method ?? "GET";
    calls.push({ url, method, body: init.body ? JSON.parse(String(init.body)) : undefined });
    const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
    if (url === "/api/v1/meta") return reply({ service_account_email: SA, share_instructions: ["Click Share."] });
    if (url === "/api/v1/sheets/access-check") return reply(accessReply);
    if (url === "/api/v1/sheets" && method === "POST") return reply({ job_id: "j1", sheet_id: 7, state: "queued" }, 202);
    if (url === "/api/v1/jobs/j1") return reply(jobStates.length > 1 ? jobStates.shift() : jobStates[0]);
    return reply({ error: { code: "not_found", message: "nope", request_id: "r" } }, 404);
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Approval() {
  return <p>approval page for config {useParams().configId}</p>;
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/enroll" element={<EnrollPage />} />
          <Route path="/approvals/:configId" element={<Approval />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("shows the service-account email and reports a forbidden access check", async () => {
  accessReply = { access: "forbidden", ok: false, sheet_id: "abc", id: 7, message: `share the sheet with ${SA} as Editor` };
  const view = renderAt("/enroll");
  await view.findByText(SA);
  fireEvent.change(view.getByLabelText("Sheet URL or ID"), { target: { value: "https://docs.google.com/spreadsheets/d/abc/edit" } });
  fireEvent.click(view.getByText("Check access"));
  await view.findByText(/No access\./);
  const check = calls.find((c) => c.url.endsWith("/access-check"))!;
  expect(check.body).toEqual({ sheet: "https://docs.google.com/spreadsheets/d/abc/edit" });
});

it("reports the title and tabs when access is ok", async () => {
  accessReply = { access: "ok", ok: true, sheet_id: "abc", id: 7, title: "Orders", tabs: ["Open", "Done"], timezone: "UTC" };
  const view = renderAt("/enroll");
  fireEvent.change(view.getByLabelText("Sheet URL or ID"), { target: { value: "abc" } });
  fireEvent.click(view.getByText("Check access"));
  await view.findByText("Orders");
  expect(view.getByText(/2 tabs: Open, Done/)).toBeTruthy();
});

it("submits, follows the compile states and opens the approval when the proposal lands", async () => {
  jobStates = [job({ state: "compiling" }), job({ state: "proposed", config_id: 42, config_version: 3, done: true })];
  const view = renderAt("/enroll");
  fireEvent.change(view.getByLabelText("Sheet URL or ID"), { target: { value: "abc" } });
  fireEvent.change(view.getByLabelText("Instruction, in plain English"), { target: { value: "sort by status" } });
  fireEvent.click(view.getByText("Compile the automation"));
  await view.findByText("Compiling the instruction");
  expect(calls.find((c) => c.url === "/api/v1/sheets")!.body).toEqual({ sheet: "abc", instruction: "sort by status" });
  await waitFor(() => expect(view.getByText("approval page for config 42")).toBeTruthy(), { timeout: 4000 });
});

it("shows the agent's readable failure for a nonsense instruction", async () => {
  jobStates = [job({
    state: "failed", done: true,
    failure: "the instruction doesn't describe anything to organize in this sheet",
    failures: [],
  })];
  const view = renderAt("/enroll?job=j1");
  await view.findByText("The instruction couldn't be compiled");
  expect(view.getByRole("alert").textContent).toBe("the instruction doesn't describe anything to organize in this sheet");
  fireEvent.click(view.getByText("Try another instruction"));
  await view.findByLabelText("Sheet URL or ID");
});
