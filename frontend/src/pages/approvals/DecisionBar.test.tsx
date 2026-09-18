import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { setToken } from "../../api/client";
import type { ConfigDetail } from "../../api/types";
import { SessionProvider } from "../../session";
import { DecisionBar } from "./DecisionBar";

const config = { id: 21, version: 20, status: "PENDING_APPROVAL" } as ConfigDetail;
let calls: Array<{ url: string; init: RequestInit }> = [];

beforeEach(() => {
  calls = [];
  setToken("t0k");
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ ...config, status: "ACTIVE" }), { status: 200 });
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function wrap(ui: ReactNode) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <SessionProvider actor="Akash" signOut={() => undefined}>
        {ui}
      </SessionProvider>
    </QueryClientProvider>,
  );
}

it("approves with the actor, the owner title and the bearer token", async () => {
  const { getByText } = wrap(<DecisionBar config={config} summaryTitle="Q3 ORDERS" />);
  fireEvent.click(getByText("Approve v20 with this title"));
  await waitFor(() => expect(calls.length).toBe(1));
  const call = calls[0]!;
  expect(call.url).toBe("/api/v1/configs/21/approve");
  expect(call.init.method).toBe("POST");
  expect(JSON.parse(String(call.init.body))).toEqual({ actor: "Akash", summary_title: "Q3 ORDERS" });
  expect((call.init.headers as Record<string, string>).Authorization).toBe("Bearer t0k");
});

it("requires a reason before rejecting and sends it", async () => {
  const { getByText, getByLabelText } = wrap(<DecisionBar config={config} summaryTitle="" />);
  fireEvent.click(getByText("Reject…"));
  const submit = getByText("Reject v20") as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  fireEvent.change(getByLabelText(/Why are you rejecting/), { target: { value: "wrong stage order" } });
  expect(submit.disabled).toBe(false);
  fireEvent.click(submit);
  await waitFor(() => expect(calls.length).toBe(1));
  expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ actor: "Akash", reason: "wrong stage order" });
});
