// Before/after sample of changed rows for one tab (PATCH-003 B1). Values come straight from the
// preview response and live only in component memory (A.3): never logged, never stored.
import type { PreviewCell, PreviewTab } from "../api/types";
import styles from "./ChangedRows.module.css";

function show(v: PreviewCell["v"]): string {
  if (v === null || v === "") return "";
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  return String(v);
}

function Cell({ cell, changed }: { cell: PreviewCell; changed: boolean }) {
  return (
    <td
      className={changed ? styles.changed : undefined}
      style={{
        color: cell.font ?? undefined,
        backgroundColor: cell.bg ?? undefined,
        textDecoration: cell.strike ? "line-through" : undefined,
      }}
    >
      {show(cell.v)}
    </td>
  );
}

export function ChangedRows({ tab }: { tab: PreviewTab }) {
  if (tab.sample.length === 0) return <p className={styles.empty}>No rows change on this tab.</p>;
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th className={styles.meta}>Row</th>
            <th className={styles.meta} />
            {tab.headers.map((h, i) => (
              <th key={i}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {tab.sample.map((r) => {
            const changed = new Set(r.changed);
            return [
              <tr key={`${r.row}-b`} className={styles.before}>
                <td className={styles.meta} rowSpan={2}>
                  {r.row}
                  {r.kind !== "data" && <span className={styles.kind}>{r.kind}</span>}
                </td>
                <td className={styles.meta}>before</td>
                {r.before.map((c, i) => (
                  <Cell key={i} cell={c} changed={changed.has(i)} />
                ))}
              </tr>,
              <tr key={`${r.row}-a`} className={styles.after}>
                <td className={styles.meta}>after</td>
                {r.after.map((c, i) => (
                  <Cell key={i} cell={c} changed={changed.has(i)} />
                ))}
              </tr>,
            ];
          })}
        </tbody>
      </table>
      {tab.changed_rows_total > tab.sample.length && (
        <p className={styles.more}>
          Showing {tab.sample.length} of {tab.changed_rows_total} changed rows.
        </p>
      )}
    </div>
  );
}
