// Follows one onboarding job (GET /jobs/{id}) through its compile states; redirects to the approval
// when the proposal lands, or shows the agent's readable failure.
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import type { Job, JobState } from "../../api/types";
import styles from "./EnrollPage.module.css";

export const STEPS: Array<{ state: JobState; label: string }> = [
  { state: "queued", label: "Queued" },
  { state: "profiling", label: "Profiling the sheet" },
  { state: "compiling", label: "Compiling the instruction" },
  { state: "validating", label: "Validating the config" },
  { state: "dry_running", label: "Dry-running on the live sheet" },
];

export const POLL_MS = 1500;

export function JobProgress({ jobId, onStartOver }: { jobId: string; onStartOver: () => void }) {
  const navigate = useNavigate();
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api<Job>(`/jobs/${encodeURIComponent(jobId)}`),
    refetchInterval: (q) => (q.state.data?.done ? false : POLL_MS),
  });
  const data = job.data;

  useEffect(() => {
    if (data?.state === "proposed" && data.config_id != null) {
      navigate(`/approvals/${data.config_id}`, { replace: true });
    }
  }, [data, navigate]);

  if (job.error) {
    const err = job.error as ApiError;
    return (
      <div className={styles.card}>
        <p className={styles.bad} role="alert">
          {err.message}
        </p>
        <button type="button" className={styles.secondary} onClick={onStartOver}>
          Start over
        </button>
      </div>
    );
  }

  if (data?.state === "failed") {
    return (
      <div className={styles.card}>
        <h2 className={styles.step}>The instruction couldn't be compiled</h2>
        <p className={styles.failure} role="alert">
          {data.failure ?? "onboarding failed"}
        </p>
        {data.failures.length > 0 && (
          <details className={styles.attempts}>
            <summary>
              {data.failures.length} attempt{data.failures.length === 1 ? "" : "s"}
            </summary>
            <ul>
              {data.failures.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          </details>
        )}
        <p className={styles.muted}>Nothing was proposed and nothing was written to the sheet.</p>
        <button type="button" className={styles.primary} onClick={onStartOver}>
          Try another instruction
        </button>
      </div>
    );
  }

  const current = data?.state ?? "queued";
  const at = STEPS.findIndex((s) => s.state === current);
  return (
    <div className={styles.card}>
      <h2 className={styles.step}>{current === "proposed" ? "Proposal ready, opening it…" : "Compiling…"}</h2>
      <ol className={styles.progress} aria-label="Compile progress">
        {STEPS.map((s, i) => {
          const status = current === "proposed" || i < at ? "done" : i === at ? "current" : "todo";
          return (
            <li key={s.state} className={styles[status]} aria-current={status === "current" ? "step" : undefined}>
              {s.label}
            </li>
          );
        })}
      </ol>
      <p className={styles.muted}>This usually takes under a minute; it can take a few if the model needs a second try.</p>
    </div>
  );
}
