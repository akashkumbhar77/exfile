# SPEC-PATCH-002.md — Fleet-scale change detection + privacy invariants

Read after CLAUDE.md, docs/SPEC.md, docs/SPEC-PATCH-001.md. Where this
conflicts with any of them, THIS PATCH WINS. Two concerns: (1) detection
that scales O(1) with fleet size, (2) privacy guarantees that must exist
before the first real byte of customer data flows through the system.

## A. Change detection (REPLACES PATCH-001 section A.5)

1. **Fleet-level Drive changes feed is the sole detection mechanism.**
   One watcher loop for the entire fleet:
   - On startup / enrollment: `changes.getStartPageToken` (Drive API v3).
   - Every POLL_INTERVAL (default 30s): `changes.list(pageToken)` with the
     service account's corpus; persist the returned `newStartPageToken`
     in a `watch_state` table (org_id, page_token, updated_at).
   - Filter results to google_sheet_ids present in `sheets` with status
     ACTIVE; ignore everything else.
   - For each matched sheet: enqueue `run:{sheet_id}` with the debounce
     policy below.
   Per-sheet polling loops are FORBIDDEN. Detection cost must not grow
   with fleet size (2 requests/min total regardless of sheet count).
2. **Debounce (unchanged in spirit, relocated):** on enqueue, delay 30s,
   re-arm on further change events for the same sheet. Before executing,
   fetch governed ranges once; compute the per-tab values fingerprint; if
   identical to the fingerprint stored from the last completed run, emit
   no ops (idempotent no-op run, logged with rows_affected=0).
3. **Self-edit suppression:** record the Drive revision/modifiedTime our
   own write produced (from the write response / an immediate metadata
   get); the watcher drops change events whose modifiedTime <= that
   watermark for that sheet. Guards the bot-triggers-bot loop under the
   changes feed just as actor-filtering did for webhooks.
4. `cli.py watch` now takes no --sheet-id: it runs the fleet watcher.
   `cli.py run --sheet-id` remains for manual/S1 use.
5. S2 exit criteria amended accordingly: the burst test and drift test
   must pass with the fleet watcher watching >= 2 enrolled sheets
   simultaneously, edits on one never triggering runs on the other.

## B. Privacy invariants (add to CLAUDE.md invariants; violating = wrong build)

6. **Raw cell values never persist outside their sheet, except in
   snapshots.** Specifically FORBIDDEN in: application logs, error
   messages and stack traces, the `runs` table, the `events` table,
   metrics, and any LLM prompt. Log cell *addresses*, counts, hashes,
   and rule ids — never contents. Add a lint-style test: grep-level
   assertion that log/exception formatting call sites in services and
   workers cannot receive Grid value objects (a `Redacted` wrapper type
   whose __str__/__repr__ yields "<redacted>"; Grid values are wrapped
   at adapter boundary, unwrapped only inside the evaluator and the
   write path).
7. **LLM masking boundary.** The agent's `sample_rows` tool masks before
   the payload leaves the process: person/company-like strings ->
   "Company_A"/"Person_B" (stable within one call), emails/phones ->
   shape-preserving dummies, numbers -> random values of same magnitude
   and format, dates -> shifted by a constant per-call offset (ordering
   preserved). Headers, tab names, and enum VALUES (status labels) pass
   through unmasked — they are structure, and rules depend on them.
   Masking lives in `app/agent/masking.py` with its own unit tests
   (given a fixture grid, assert no original free-text value appears in
   the serialized tool result). Profiles stored in the `profiles` table
   contain structure and distributions only, never sampled row values.
8. **Snapshots: encrypted, expiring.** `snapshots.body_gz` is encrypted
   at rest (Fernet; key from env `SNAPSHOT_KEY`, per-org keys later —
   key id column now). New org-level setting
   `snapshot_retention_days` (default 30); a daily job purges expired
   snapshots. Undo of a purged snapshot fails with a clear message.
9. **Access scoping:** the service account is granted per-sheet by the
   customer; the system must never call Drive listing/search endpoints
   beyond the changes feed, and must never open a file id that is not in
   the `sheets` registry. (Future note, do not build: public OAuth =
   drive.file + Picker only.)

## C. Build order

Implement A inside S2 (it replaces S2's polling task). Implement B.6–B.8
before S2's first live `watch` run against the real workbook; B.7 lands
with S3 (it is part of the agent's sample path, tested before the first
real LLM call that includes samples). Add a docs/DATA-FLOW.md one-pager
(generated as part of B) describing: what is read, what is stored
(metadata, configs, encrypted expiring snapshots), what reaches the LLM
(masked samples + structure), and the purge path — written for a
customer's IT reviewer, not for us.
