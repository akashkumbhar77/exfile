// Approve / Reject (PATCH-003 B1). Reject requires a reason, which is kept in config history.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../../api/client";
import type { ConfigDetail } from "../../api/types";
import { useSession } from "../../session";
import styles from "./DecisionBar.module.css";

export function DecisionBar({ config, summaryTitle }: { config: ConfigDetail; summaryTitle: string }) {
  const { actor } = useSession();
  const qc = useQueryClient();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  const done = () => {
    void qc.invalidateQueries({ queryKey: ["configs"] });
    void qc.invalidateQueries({ queryKey: ["config", config.id] });
  };
  const approve = useMutation({
    mutationFn: () =>
      api<ConfigDetail>(`/configs/${config.id}/approve`, {
        method: "POST",
        body: { actor, summary_title: summaryTitle || null },
      }),
    onSuccess: done,
  });
  const reject = useMutation({
    mutationFn: () =>
      api<ConfigDetail>(`/configs/${config.id}/reject`, { method: "POST", body: { actor, reason: reason.trim() } }),
    onSuccess: done,
  });
  const error = (approve.error ?? reject.error) as ApiError | null;
  const busy = approve.isPending || reject.isPending;

  return (
    <div className={styles.bar} aria-label="Decision">
      {error && (
        <p className={styles.error} role="alert">
          {error.message}
          {error.requestId && <span className={styles.rid}> (request {error.requestId})</span>}
        </p>
      )}
      {!rejecting ? (
        <div className={styles.actions}>
          <button type="button" className={styles.approve} disabled={busy} onClick={() => approve.mutate()}>
            Approve v{config.version}
            {summaryTitle ? " with this title" : ""}
          </button>
          <button type="button" className={styles.secondary} disabled={busy} onClick={() => setRejecting(true)}>
            Reject…
          </button>
          <span className={styles.who}>as {actor}</span>
        </div>
      ) : (
        <form
          className={styles.rejectForm}
          onSubmit={(e) => {
            e.preventDefault();
            reject.mutate();
          }}
        >
          <label htmlFor="reject-reason" className={styles.label}>
            Why are you rejecting this config? <span className={styles.required}>(required, kept in history)</span>
          </label>
          <textarea
            id="reject-reason"
            className={styles.textarea}
            rows={3}
            maxLength={1000}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className={styles.actions}>
            <button type="submit" className={styles.reject} disabled={busy || !reason.trim()}>
              Reject v{config.version}
            </button>
            <button type="button" className={styles.secondary} disabled={busy} onClick={() => setRejecting(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
