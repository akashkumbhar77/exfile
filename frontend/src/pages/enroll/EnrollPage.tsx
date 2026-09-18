// Enroll (PATCH-003 B2): sheet URL or id + plain-English instruction -> onboarding job -> approval.
// The job id lives in the URL (?job=) so a reload keeps following the same compile.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import type { AccessCheck, EnrollAccepted, Meta } from "../../api/types";
import { JobProgress } from "./JobProgress";
import styles from "./EnrollPage.module.css";

export function EnrollPage() {
  const [params, setParams] = useSearchParams();
  const jobId = params.get("job");
  const [sheet, setSheet] = useState("");
  const [instruction, setInstruction] = useState("");

  const meta = useQuery({ queryKey: ["meta"], queryFn: () => api<Meta>("/meta"), staleTime: Infinity });
  const access = useMutation({
    mutationFn: (ref: string) => api<AccessCheck>("/sheets/access-check", { method: "POST", body: { sheet: ref } }),
  });
  const enroll = useMutation({
    mutationFn: () =>
      api<EnrollAccepted>("/sheets", { method: "POST", body: { sheet: sheet.trim(), instruction: instruction.trim() } }),
    onSuccess: (r) => setParams({ job: r.job_id }),
  });

  const checkedFor = access.variables;
  const accessResult = access.data && checkedFor === sheet.trim() ? access.data : null;
  const submitError = enroll.error as ApiError | null;
  const accessError = access.error as ApiError | null;
  const canSubmit = sheet.trim() !== "" && instruction.trim() !== "" && !enroll.isPending;

  if (jobId) {
    return (
      <main className={styles.page}>
        <h1 className={styles.title}>Enroll a sheet</h1>
        <JobProgress jobId={jobId} onStartOver={() => setParams({})} />
      </main>
    );
  }

  return (
    <main className={styles.page}>
      <h1 className={styles.title}>Enroll a sheet</h1>

      <section className={styles.card} aria-labelledby="share-heading">
        <h2 id="share-heading" className={styles.step}>
          1. Share the sheet with the service account
        </h2>
        {meta.data?.service_account_email ? (
          <p className={styles.email}>
            <code>{meta.data.service_account_email}</code>
            <CopyButton text={meta.data.service_account_email} />
          </p>
        ) : (
          <p className={styles.muted}>{meta.isError ? "Couldn't load the service-account email." : "Loading…"}</p>
        )}
        <ol className={styles.instructions}>
          {(meta.data?.share_instructions ?? []).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ol>
      </section>

      <form
        className={styles.card}
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) enroll.mutate();
        }}
      >
        <h2 className={styles.step}>2. Which sheet?</h2>
        <label htmlFor="sheet" className={styles.label}>
          Sheet URL or ID
        </label>
        <div className={styles.row}>
          <input
            id="sheet"
            className={styles.input}
            value={sheet}
            maxLength={500}
            placeholder="https://docs.google.com/spreadsheets/d/…"
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setSheet(e.target.value)}
          />
          <button
            type="button"
            className={styles.secondary}
            disabled={!sheet.trim() || access.isPending}
            onClick={() => access.mutate(sheet.trim())}
          >
            {access.isPending ? "Checking…" : "Check access"}
          </button>
        </div>
        {accessResult && <AccessResult result={accessResult} />}
        {accessError && checkedFor === sheet.trim() && (
          <p className={styles.bad} role="alert">
            {accessError.message}
          </p>
        )}

        <h2 className={styles.step}>3. What should the automation do?</h2>
        <label htmlFor="instruction" className={styles.label}>
          Instruction, in plain English
        </label>
        <textarea
          id="instruction"
          className={styles.textarea}
          rows={8}
          maxLength={4000}
          value={instruction}
          placeholder="e.g. Sort each tab by Status in this order: New, In progress, Done. Grey out Done rows."
          onChange={(e) => setInstruction(e.target.value)}
        />
        <p className={styles.muted}>
          Nothing is written to the sheet from this page. You'll review a dry-run preview and approve it first.
        </p>

        {submitError && (
          <p className={styles.bad} role="alert">
            {submitError.message}
          </p>
        )}
        <div>
          <button type="submit" className={styles.primary} disabled={!canSubmit}>
            {enroll.isPending ? "Starting…" : "Compile the automation"}
          </button>
        </div>
      </form>
    </main>
  );
}

function AccessResult({ result }: { result: AccessCheck }) {
  if (!result.ok) {
    return (
      <p className={styles.bad} role="status">
        {result.access === "forbidden" ? "No access. " : "Not found. "}
        {result.message}
      </p>
    );
  }
  return (
    <p className={styles.good} role="status">
      Access OK: <strong>{result.title || "untitled spreadsheet"}</strong>
      {result.tabs.length > 0 && (
        <span className={styles.muted}>
          {" "}
          ({result.tabs.length} tab{result.tabs.length === 1 ? "" : "s"}: {result.tabs.join(", ")})
        </span>
      )}
    </p>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className={styles.copy}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(
          () => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          },
          () => setCopied(false),
        );
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
