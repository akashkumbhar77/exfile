// /approvals and /approvals/:configId (PATCH-003 B1): pending configs on the left, the selected
// one's readback, preview and decision on the right. The list polls every 5 s.
import { useQuery } from "@tanstack/react-query";
import { NavLink, useParams } from "react-router-dom";
import { api } from "../../api/client";
import type { ConfigSummary } from "../../api/types";
import { ApprovalDetail } from "./ApprovalDetail";
import styles from "./ApprovalsPage.module.css";

export function ApprovalsPage() {
  const { configId } = useParams();
  const pending = useQuery({
    queryKey: ["configs", "PENDING_APPROVAL"],
    queryFn: () => api<{ items: ConfigSummary[] }>("/configs", { query: { status: "PENDING_APPROVAL" } }),
    refetchInterval: 5000,
  });
  const items = pending.data?.items ?? [];
  const selected = configId ? Number(configId) : items[0]?.id;

  return (
    <div className={styles.layout}>
      <aside className={styles.list} aria-label="Pending approvals">
        <h2 className={styles.heading}>Pending approvals</h2>
        {pending.isLoading && <p className={styles.muted}>Loading…</p>}
        {pending.error && <p className={styles.error}>{(pending.error as Error).message}</p>}
        {!pending.isLoading && items.length === 0 && !pending.error && (
          <p className={styles.muted}>Nothing waiting for approval.</p>
        )}
        <ul className={styles.items}>
          {items.map((c) => (
            <li key={c.id}>
              <NavLink
                to={`/approvals/${c.id}`}
                className={({ isActive }) =>
                  isActive || (!configId && c.id === selected) ? `${styles.item} ${styles.active}` : styles.item
                }
              >
                <span className={styles.itemTitle}>{c.sheet?.title ?? `sheet ${c.sheet_id}`}</span>
                <span className={styles.itemMeta}>
                  v{c.version} · {c.rule_count} rules · {c.source.kind === "onboarding" ? `by ${c.source.model}` : "hand-written"}
                </span>
              </NavLink>
            </li>
          ))}
        </ul>
      </aside>
      <section className={styles.detail}>
        {selected !== undefined ? <ApprovalDetail key={selected} configId={selected} /> : null}
      </section>
    </div>
  );
}
