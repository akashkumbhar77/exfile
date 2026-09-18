// Plain-English readback of a config, rendered from GET /configs/{id}/describe.
// The text is produced by backend templates (PATCH-003 A.2); this component only lays it out.
import type { Readback as ReadbackData, Segment } from "../api/types";
import { Swatch } from "./Swatch";
import styles from "./Readback.module.css";

const ACTION_LABEL: Record<string, string> = {
  sort: "Sort",
  format: "Format",
  consolidate: "Consolidate",
  move: "Move",
  copy: "Copy",
  validate: "Dropdown",
  dedupe: "Dedupe",
  clear: "Clear",
};

function Segments({ segments }: { segments: Segment[] }) {
  return (
    <>
      {segments.map((s, i) =>
        s.kind === "color" ? <Swatch key={i} name={s.name} hex={s.hex} /> : <span key={i}>{s.text}</span>,
      )}
    </>
  );
}

export function Readback({ data }: { data: ReadbackData }) {
  return (
    <section className={styles.readback} aria-label="What this config will do">
      <p className={styles.summary}>{data.summary}</p>
      {!data.live && <p className={styles.notice}>The sheet could not be read just now; tab lists are not live.</p>}
      {data.tabs_missing.length > 0 && (
        <p className={styles.warning}>Missing from the sheet now: {data.tabs_missing.join(", ")}</p>
      )}

      {data.stages.length > 0 && (
        <div className={styles.block}>
          <h3 className={styles.heading}>Stages</h3>
          {data.stages.map((s) => (
            <p key={s} className={styles.stage}>
              {s}
            </p>
          ))}
        </div>
      )}

      <div className={styles.block}>
        <h3 className={styles.heading}>Rules</h3>
        <ol className={styles.rules}>
          {data.rules.map((r) => (
            <li key={r.id} className={styles.rule} data-rule={r.number}>
              <div className={styles.ruleHead}>
                <span className={styles.number}>{r.number}</span>
                <span className={styles.action}>{ACTION_LABEL[r.action] ?? r.action}</span>
                <span className={styles.headline}>{r.headline}</span>
              </div>
              <p className={styles.when}>When: {r.when}</p>
              {r.details.length > 0 && (
                <ul className={styles.details}>
                  {r.details.map((d, i) => (
                    <li key={i}>
                      <Segments segments={d.segments} />
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ol>
      </div>

      <div className={styles.block}>
        <h3 className={styles.heading}>Safety</h3>
        <ul className={styles.details}>
          {data.guards.map((g) => (
            <li key={g}>{g}</li>
          ))}
        </ul>
      </div>
    </section>
  );
}
