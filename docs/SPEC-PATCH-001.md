# SPEC-PATCH-001.md — Locked forks + walking-skeleton plan

Read after CLAUDE.md and docs/SPEC.md. Where this patch conflicts with them,
THIS PATCH WINS. Purpose: prove the Google Sheets flow end-to-end on a real
workbook as fast as possible, without violating invariants.

## A. Architecture decisions now LOCKED (supersede prior spec text)

1. **Server-side Python engine is the ONLY runtime.** There is no Engine.gs.
   Delete/ignore SPEC section 5 and all Apps Script engine work. The M1
   evaluator is the single implementation of rule semantics for dry-run,
   live execution, and undo. (CLAUDE.md invariant 3 is replaced by: "One
   server-side engine; rule semantics exist in exactly one code path.")
2. **Adapter interface.** All format-specific code lives in
   `app/adapters/` implementing:
   - `read_grid(source_ref) -> Grid` (values + formats + tab metadata)
   - `write_ops(source_ref, ops: list[GridOp]) -> WriteResult`
   - `detect_changes(source_ref) -> ChangeInfo`
   - `capabilities() -> AdapterCaps` (e.g. realtime: bool, formats: [...])
   Build ONLY `sheets_adapter` now. The xlsx parity test may reuse a
   read-only xlsx adapter if convenient, but it is not a deliverable.
3. **Rule configs are format-agnostic.** No adapter-specific fields in
   ConfigSpec.
4. **Auth for pilot = service account only.** No OAuth flows of any kind.
   (Future note, do not build: public auth will use drive.file + Picker;
   never request `spreadsheets` or `script.projects` scopes.)
5. **Change detection for pilot = polling.** No webhooks, no Apps Script
   pings. `detect_changes` = compare a cheap fingerprint (per-tab
   values checksum via one batchGet of governed ranges) every
   POLL_INTERVAL (default 30s), debounced: act only after one stable
   interval (fingerprint unchanged since last poll) to avoid racing a
   user mid-typing.

## B. Parked (do NOT build until un-parked in a future patch)

Webhooks & Apps Script anything · drift pause path & repair agent (M6) ·
weekly audit · anomaly thresholds · React dashboard (M7) · Excel/Graph
adapters · billing/orgs UI · Marketplace/OAuth. Keep `org_id` columns and
Class B/C flag tables in place, but only the schema — no behavior.

Two safety behaviors are NOT parked (they are invariants, not features):
pre-flight header-hash check before every run (mismatch → mark sheet
PAUSED_DRIFT, skip run, log — no repair flow yet), and snapshots before
destructive ops.

## C. Walking-skeleton milestones (replace M2–M4; M1 stands as done)

### S1 — Organize on command (the "it works" milestone)
Build: `sheets_adapter.read_grid` + `write_ops` (values reorder, font
color, strikethrough, background fill, via values.batchGet +
spreadsheets.batchUpdate); GridOp translation from M1 evaluator output;
CLI: `cli.py run --sheet-id X --config path.json [--dry-run]`.
Config: the hand-written config for the reference workbook (from the M1
parity fixtures) is the input — no agent involved.
Exit criteria:
- `--dry-run` prints per-tab summary (rows to reorder, cells to recolor)
  with zero writes (verify via Drive revision history unchanged).
- Live run on the converted Google Sheet copy produces byte-for-byte the
  sort order and formatting the legacy xlsx export shows (reuse the M1
  parity assertions, now reading back through the adapter).
- Second consecutive run is a no-op (idempotency: 0 ops emitted).

### S2 — Reacts to edits + undo
Build: polling loop (`cli.py watch --sheet-id X`), fingerprint + debounce
per A.5; runs + snapshots + events tables wired (registry can be minimal:
sheets/configs/runs/snapshots); `cli.py undo --run-id Y` restores the
before-snapshot exactly; pre-flight hash check active.
Exit criteria:
- Manually flip a STATUS cell → within ~90s the tab re-sorts and recolors;
  logs show exactly one run for a burst of several quick edits.
- Rename a header → next poll marks PAUSED_DRIFT, runs nothing, logs it;
  restoring the header + `cli.py resume` recovers.
- `undo` after a destructive run restores prior values verbatim.

### S3 — Plain English in front
Build: onboarding agent per SPEC section 6 unchanged (profile → compile →
validate loop w/ haiku→sonnet escalation → dry-run), CLI approval:
`cli.py enroll --sheet-id X --instruction "..."` prints dry-run summary,
asks y/n, activates on y.
Exit criteria:
- The reference workbook enrolled from a plain-English instruction alone
  reaches the same ACTIVE config behavior as the hand-written one (dry-run
  summaries equivalent).
- A nonsense instruction ends in a human-readable failure, never an
  ACTIVE config.

Definition of "the Sheets flow works": S1+S2+S3 all green on the real
converted workbook, then one week of `watch` running against it while the
owner edits normally, with zero invariant violations in the runs log.

## D. Operator setup (human-only; Claude Code: stub config from env and
document these in README, do not attempt them)
GCP project → enable Google Sheets API + Google Drive API → service
account → JSON key at path in `GOOGLE_APPLICATION_CREDENTIALS` → test
sheet shared to the service-account email as Editor → its ID in
`.env TEST_SHEET_ID`.
