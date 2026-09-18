// Minimal fetch wrapper for /api/v1.
// PATCH-003 C: the bearer token is held in memory only (module variable set by the session gate),
// never in any browser storage. PATCH-003 A.3: responses (which may carry cell values in
// previews) are returned to the caller and never logged or stored.
import type { ErrorEnvelope } from "./types";

export const API_BASE = "/api/v1";

let bearer: string | null = null;

export function setToken(token: string | null): void {
  bearer = token && token.trim() ? token.trim() : null;
}

export function hasToken(): boolean {
  return bearer !== null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: { method?: "GET" | "POST"; body?: unknown; query?: Record<string, string | undefined> }): Promise<T> {
  const url = new URL(API_BASE + path, window.location.origin);
  for (const [k, v] of Object.entries(init?.query ?? {})) {
    if (v !== undefined && v !== "") url.searchParams.set(k, v);
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  if (init?.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(url.toString().replace(window.location.origin, ""), {
    method: init?.method ?? "GET",
    headers,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  const payload: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const env = payload as ErrorEnvelope | null;
    throw new ApiError(
      res.status,
      env?.error?.code ?? "error",
      env?.error?.message ?? `request failed (${res.status})`,
      env?.error?.request_id ?? null,
    );
  }
  return payload as T;
}
