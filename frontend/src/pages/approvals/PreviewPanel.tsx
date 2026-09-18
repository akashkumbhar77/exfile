// Dry-run summary per tab (rows to reorder / recolor / move, warnings) + changed-rows sample.
import type { UseQueryResult } from "@tanstack/react-query";
import type { Preview } from "../../api/types";
import { ChangedRows } from "../../components/ChangedRows";
import styles from "./PreviewPanel.module.css";

export function PreviewPanel({ query }: { query: UseQueryResult<Preview> }) {
  if (query.isLoading) return <p className={styles.muted}>Running the dry run on the live sheet…</p>;
  if (query.error || !query.data) return <p className={styles.error}>{(query.error as Error)?.message ?? "No preview"}</p>;
  const p = query.data;
  const changing = p.tabs.filter((t) => t.changed_rows_total > 0);

  return (
    <div className={styles.panel}>
      <p className={styles.status}>
        {p.status !== "OK"
          ? `Dry run status ${p.status} — nothing would be written.`
          : p.ops === 0
            ? "Approving changes nothing in the sheet right now: it already matches this config."
            : `Approving would update ${changing.length} tab${changing.length === 1 ? "" : "s"} on the next run.`}{" "}
        <span className={styles.muted}>
          (as of today, {p.today}, {p.timezone})
        </span>
      </p>

      {p.drift.length > 0 && (
        <p className={styles.warning}>Header changed on: {p.drift.map((d) => d.tab).join(", ")}</p>
      )}
      {p.warnings.length > 0 && (
        <ul className={styles.warnings}>
          {p.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      <table className={styles.counts}>
        <thead>
          <tr>
            <th>Tab</th>
            <th>Rows to reorder</th>
            <th>Rows to recolor</th>
            <th>Rows to move</th>
            <th>Cells to write</th>
            <th>Cells to recolor</th>
          </tr>
        </thead>
        <tbody>
          {p.tabs.map((t) => (
            <tr key={t.tab}>
              <th scope="row">
                {t.tab}
                {!t.exists && <span className={styles.newTab}> (new)</span>}
              </th>
              <td>{t.rows_to_reorder}</td>
              <td>{t.rows_to_recolor}</td>
              <td>{t.rows_added + t.rows_removed}</td>
              <td>{t.cells_to_write}</td>
              <td>{t.cells_to_recolor}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {changing.map((t) => (
        <details key={t.tab} className={styles.tab} open={changing.length <= 2}>
          <summary>
            {t.tab}: {t.changed_rows_total} changed row{t.changed_rows_total === 1 ? "" : "s"}
          </summary>
          <ChangedRows tab={t} />
        </details>
      ))}
    </div>
  );
}
