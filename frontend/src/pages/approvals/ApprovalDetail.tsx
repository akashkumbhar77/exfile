// One pending config: readback (live tabs), dry-run preview with before/after rows, optional
// summary-tab title (PATCH-003 decision (c): owner-supplied, never model-read), approve/reject.
// Describe and preview are fetched on view and kept only in query memory (gcTime 0): no polling,
// no persistence of preview data (A.3).
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { ConfigDetail, FirstRun, Preview, Readback as ReadbackData } from "../../api/types";
import { Readback } from "../../components/Readback";
import { DecisionBar } from "./DecisionBar";
import { PreviewPanel } from "./PreviewPanel";
import styles from "./ApprovalDetail.module.css";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

// Approval queues exactly one run that applies the previewed changes (DECISIONS "Approval queues
// the first run"); this line follows it from the config's first_run.
export function firstRunText(run: FirstRun | null): string {
  if (!run) return "The changes apply on the sheet's next edit.";
  if (run.state === "queued") return "Applying the changes now…";
  switch (run.status) {
    case "OK":
      return `Changes applied (${run.rows_affected} row${run.rows_affected === 1 ? "" : "s"} updated). The run can be undone from the sheet's history.`;
    case "NOOP":
      return "Applied: the sheet already matched, so nothing needed changing.";
    case "PAUSED_DRIFT":
      return "Not applied: the sheet's headers changed since the preview, so automation is paused for this sheet.";
    case "STALE":
      return "Not applied yet: someone edited the sheet while it was being planned. It retries automatically once they stop editing.";
    default:
      return `The first run ended with status ${run.status}; nothing was half-applied. See the sheet's history.`;
  }
}

export function ApprovalDetail({ configId }: { configId: number }) {
  const [title, setTitle] = useState("");
  const [previewTitle, setPreviewTitle] = useState("");
  const debouncedTitle = useDebounced(title.trim(), 400);

  const config = useQuery({
    queryKey: ["config", configId],
    queryFn: () => api<ConfigDetail>(`/configs/${configId}`),
    refetchInterval: (q) => (q.state.data?.first_run?.state === "queued" ? 1500 : 5000),
  });
  const readback = useQuery({
    queryKey: ["describe", configId, debouncedTitle],
    queryFn: () => api<ReadbackData>(`/configs/${configId}/describe`, { query: { summary_title: debouncedTitle } }),
    staleTime: Infinity,
    gcTime: 0,
    placeholderData: (prev) => prev,
  });
  const preview = useQuery({
    queryKey: ["preview", configId, previewTitle],
    queryFn: () => api<Preview>(`/configs/${configId}/dry-run/preview`, { query: { summary_title: previewTitle } }),
    staleTime: Infinity,
    gcTime: 0,
  });

  if (config.isLoading) return <p className={styles.muted}>Loading…</p>;
  if (config.error || !config.data) return <p className={styles.error}>{(config.error as Error)?.message ?? "Not found"}</p>;
  const c = config.data;
  const decided = c.status !== "PENDING_APPROVAL";

  return (
    <article className={styles.detail}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>{c.sheet.title}</h1>
          <p className={styles.meta}>
            Config v{c.version} ·{" "}
            {c.source.kind === "onboarding" ? `compiled by ${c.source.model} from plain English` : "hand-written"} ·
            proposed {c.created_at ? new Date(c.created_at).toLocaleString() : ""}
          </p>
        </div>
        <span className={`${styles.status} ${styles[c.status.toLowerCase()] ?? ""}`}>{c.status.replace("_", " ")}</span>
      </header>

      {decided && (
        <p className={styles.decided} role="status">
          {c.status === "REJECTED"
            ? `Rejected by ${c.rejected_by}: ${c.decision_reason}`
            : `Approved by ${c.approved_by}${c.summary_title ? ` with title “${c.summary_title}”` : ""}. ${firstRunText(c.first_run)}`}
        </p>
      )}

      <h2 className={styles.section}>What it will do</h2>
      {readback.error && <p className={styles.error}>{(readback.error as Error).message}</p>}
      {readback.data ? <Readback data={readback.data} /> : <p className={styles.muted}>Loading readback…</p>}

      {c.has_consolidate && !decided && (
        <div className={styles.titleBox}>
          <label className={styles.label} htmlFor="summary-title">
            Summary tab title <span className={styles.optional}>(optional)</span>
          </label>
          <div className={styles.titleRow}>
            <input
              id="summary-title"
              className={styles.input}
              value={title}
              maxLength={200}
              placeholder="Leave empty to keep the tab's current title"
              onChange={(e) => setTitle(e.target.value)}
            />
            <button
              type="button"
              className={styles.secondary}
              disabled={title.trim() === previewTitle}
              onClick={() => setPreviewTitle(title.trim())}
            >
              Update preview
            </button>
          </div>
        </div>
      )}

      <h2 className={styles.section}>Dry run on the live sheet</h2>
      <PreviewPanel query={preview} />

      {!decided && <DecisionBar config={c} summaryTitle={title.trim()} />}
    </article>
  );
}
