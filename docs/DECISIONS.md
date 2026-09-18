# DECISIONS.md

Choices made where SPEC.md was silent or ambiguous. Each one takes the
simplest option that keeps the CLAUDE.md invariants. Newest milestone last.

## M1 — Config schema, validator, evaluator, dry-run

### Repo housekeeping
- Moved `doc/SPEC.md` to `docs/SPEC.md` and renamed `engine/reference/leagcy.gs`
  to `legacy.gs`. Both now match the paths in CLAUDE.md and SPEC.md.
- Python 3.12 is pinned via uv (`backend/.python-version`, `requires-python`).
  M1 depends only on pydantic. FastAPI, SQLAlchemy and the rest arrive in M3/M4.

### Code layout
- The evaluator lives in `app/services/rules/` (one module per action family).
  `app/services/runner.py::plan_run` is the only path from config + workbook
  to plans. `dry_run.py` summarizes it and `executor.py` snapshots and commits
  it. Planning logic is not duplicated anywhere.
- The validator is `app/services/validator.py`. Pydantic handles shape, and a
  second pass handles cross-references, so every error carries an RFC 6901
  pointer and a stable `code`.
- Union tags are namespaced (`cond:*`, `trigger:*`) so validator pointers never
  confuse a tag with a real key.

### Schema additions (not shown in the SPEC example, all optional)
| Addition | Why |
|---|---|
| `SortKey.type` (`auto\|date\|number\|text`) | Needed to sort dates the way legacy does without guessing from values |
| `FormatRule.default` (neutral: black, no fill, no strike) | Invariant 9: rows no rule matches get neutral formatting |
| `ConsolidateRule.format_like` | Legacy styles SUMMARY exactly like the category tabs |
| `ConsolidateRule.presentation` | Title banner, banding, widths, date format. Engine.gs applies these in M2; Python ignores them |
| `Style.background: "none"` | Tells "clear the fill" (legacy) apart from "leave it unchanged" (`null`) |
| `EnumCondition.column` | Defaults to the enum's name, as in the SPEC example |
| Triggers: `schedule: {cron}` | SPEC §4 mentions cron rules |
| `Guards.snapshot_destructive` is `Literal[True]` | Invariant 6 must not be configurable |

Minimal shapes for actions the SPEC names but doesn't specify:
- **move**: `when`, `to_tab`, `position` (`top|bottom`).
- **copy**: same as move, plus a required `key_columns` so reruns stay idempotent.
- **validate**: `column` plus exactly one of `values` / `from_enum`, and `allow_invalid`.
- **dedupe**: `key_columns`, `keep` (`first|last`).
- **clear**: `when` plus a required `columns`. It blanks cells and never deletes rows.

### Matching and columns
- Match expressions are `contains:X` / `equals:X`, trimmed and case-insensitive.
  The needle must not be blank.
- A header maps to the first `canonical_headers` entry that matches. If none
  matches, it passes through as its trimmed text. Column identity is
  trimmed + upper-cased.
- If several columns in one tab map to the same canonical name, rules use the
  **first** one. Legacy used the last; with the reference data this makes no
  difference. Consolidation still merges all of them (first non-empty wins),
  like legacy.
- Enum stages are matched in **list order**, not by `order`, so
  "DISPATCHED" is checked after "DISPUTED", as in legacy. Blank values never
  match a stage.
- A rule that references a column its tab lacks does nothing for that tab and
  records a warning (legacy behaviour: missing dispatch column = skip that key).

### Tab selection and governance
- Only tabs listed in `schema_hashes` are governed. A tab that matches
  `all_with:` but has no hash is skipped with a warning, because it can't be
  drift-checked.
- Consolidate targets are never rule inputs and must not appear in
  `schema_hashes`. Their headers are generated, so hashing them would pause the
  sheet whenever a source gains a column.

### Pre-flight hash (Engine.gs must match this exactly)
- Take the header-row cells, trim each (case is kept), drop trailing blanks,
  join with U+001F, then SHA-256 as lowercase hex.
- Any mismatch or missing governed tab gives `PAUSED_DRIFT` for the whole
  sheet, and nothing is evaluated.

### Evaluation semantics
- **Hold rows** (`guards.hold_column` header, case-insensitive) are never
  candidates for any rule. In a sort they keep their absolute position. Their
  formatting and dropdowns are left untouched. Consolidate, move, copy, dedupe
  and clear skip them.
  - The marker counts as **set** for: TRUE, a non-zero number, a date, or any
    text except FALSE/NO/0.
- **Sort**: stable and multi-key.
  - Enum keys use the stage order. Unknown and blank values get
    `unknown_order`, and `blanks` is ignored.
  - Blanks are placed first or last regardless of `asc`/`desc`.
  - `date` keys accept real dates and ISO `YYYY-MM-DD` text. Anything else
    counts as blank; legacy's `new Date(str)` parsing can't be reproduced.
  - Values move but formats stay put, exactly like `setValues`.
- **Format**: styles are applied in this order, with the last one winning for
  each attribute:
  1. the current format
  2. `default`
  3. matching `row_rules`, in order
  4. matching `cell_rules`, in order

  The range is the data region **as it was at run start**. legacy.gs formats
  its pre-sort range, so blank rows that a sort pushes to the bottom are reset
  to neutral. Title and header rows are never touched.
- **Date conditions**: only real date cells count.
  - `before D` means earlier than D at 00:00.
  - `after D` means on or after the following midnight.
  - "today" comes from the run context (the sheet's timezone).
- **Value conditions** compare trimmed, upper-cased text. In `cell_rules` a
  condition with no `column` applies to that cell.
- **Consolidate** follows legacy exactly:
  - The header is the union of canonical headers, in first-seen order.
  - Derived columns are appended if missing, and prepended columns go first.
  - Empty rows and held rows are skipped.
  - The target is fully regenerated, placed first among the tabs and locked.
    Locking is the by-design exception to invariant 8.
  - The hold column is excluded.
- **Move/copy** align values by canonical header. A non-empty value with no
  matching target column **aborts the rule**: data is never dropped.
- **Dedupe** never removes a row whose key cells are all empty.
- **Validate** never changes values; it only warns about values not on the list.

### Guards, atomicity, snapshots
- `max_rows_per_run` counts rows affected by the data-changing actions (sort,
  move, copy, dedupe, clear), summed across the run. Format, validate and
  consolidate don't count, since they don't change user data values.
- The executor applies all or nothing: if any rule errors or is blocked, the
  whole run is refused and the input workbook is left unchanged.
- Every tab a run changes gets a before-snapshot (gzip JSON of values, formats,
  validations and protection), not only destructive changes, so every run can
  be undone. A tab the run created is recorded with `existed=false`, and undo
  deletes it.
- Undo restores a run's snapshots newest-first, which returns the workbook to
  its exact pre-run state (tested).

### Legacy quirks not reproduced
- Legacy skips sorting and formatting on tabs with only one data row. The new
  engine formats them too.
- Legacy picks the *last* duplicate header; see "Matching and columns" above.

## M2 — Engine.gs

### Scope and verification
- `Engine.gs` is a single plain-V8 file. Its semantics mirror the Python evaluator
  (`backend/app/services/rules`), so a backend dry-run predicts what the engine does.
- Exit criterion (parity with legacy.gs): verified against an in-memory Apps Script
  mock (`engine/test/gas_mock.js`). Both scripts run in real V8 via Node. The checks
  are the reference workbook, 150 random workbooks and an edit sequence.
- Parity on the *real* Google workbook still has to be checked by hand, because this
  environment can't reach Google. `engine/tools/CompareWorkbooks.gs` does the
  comparison (steps in `engine/CONFIG.md`).
- `backend/tests/test_engine_python_parity.py` runs Engine.gs and the Python
  evaluator on 120 random workbook + config pairs, covering all 8 actions, hold
  rows, guards, aborts and edit events.
- The mock copies these Sheets behaviours: `getLastRow` semantics, no fill reported
  as `#ffffff`, strict array dimensions, grid bounds, row insert/delete shifting,
  the 9 KB property limit, and a fresh script evaluation per execution.

### Clarifications made while matching legacy.gs
- **`on_edit.columns` in the SPEC example** now includes `FREEZE?`.
  - legacy re-formats on every edit, and the chained format rule reads FREEZE?.
  - Rule: every column a chained rule depends on must fire the chain.
  - SPEC.md §1 is updated to say so.
- **Reference config `presentation.column_widths`** now carries legacy's full width
  map. Widths are config data, not engine constants.
- **`date_format: "match_source"`** takes the number format of the first data cell of
  the `sort_like` rule's first `type: date` key column, in the first source tab that
  has data. Legacy hard-coded "the header containing DISPATCH". If there is no such
  key, the engine uses the first canonical header containing DATE.
- **Python alignment** so both implementations are exactly reproducible:
  - Date text is only recognised as `YYYY-MM-DD`.
  - Numbers must match `^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$`.
  - Text sort keys use `lower()`.
  - An edit on *any* column that maps to a canonical name counts as that column.

### Engine behaviour
- **Triggers:** installable only — `engineOnEdit`, `engineTick` (1 min) and
  `engineOnOpen` (menu). There are no simple triggers. `engineInstall` also removes
  triggers whose handler no longer exists, such as legacy's.
- **Run pipeline:**
  1. Check status.
  2. Check the edited tab is governed.
  3. Pre-flight hash of **all** governed tabs.
  4. Evaluate every selected rule in memory.
  5. Check guards.
  6. Snapshot.
  7. Write.
  8. Queue run records.
  9. Flush the queue (best effort).
- **Never half-apply:**
  - Evaluation finishes before any write, and any rule abort or guard refusal means
    nothing is written.
  - If a write fails, every tab already written is restored from the in-memory
    before-state, and all rules are logged `ERROR` with a rollback note.
  - Consolidate targets are written only after user tabs commit. A failed rebuild
    re-arms the dirty flag.
- **Writes:**
  - Structural operations are replayed first (`deleteRows` in contiguous runs from
    the bottom, `insertRowsBefore`). Appending below the data needs no insert.
  - Values are written for runs of changed rows only. Unchanged rows, including held
    rows, are never rewritten, so their formulas survive.
  - Formats are written only for runs of rows a format rule changed.
  - Dropdowns are written in contiguous runs.
- **Formula flattening:** rows the engine rewrites are written with `setValues`, which
  flattens formulas in those rows. legacy.gs flattened every data row.
- **Snapshots:**
  - One gzip+base64 row per changed user tab per run, in the hidden
    `_engine_snapshots` tab, before any write. The last 25 runs are kept.
  - `engineUndoRun`/`engineUndoLastRun` restore values, font colours, font lines and
    fills, then mark the affected consolidations dirty.
  - Consolidate targets are not snapshotted; they are rebuilt from the sources.
  - A restore writes "no fill" for cells that read back `#ffffff`, and underline is
    not preserved.
  - Dropdowns are not part of snapshots.
  - Uploading snapshots to Postgres is M3.
- **Concurrency:**
  - A script lock guards every mutation.
  - An edit that can't get the lock is recorded in `engine.pending_tabs` and replayed
    by the next tick (running all of that tab's `on_edit` rules).
- **Debounce:**
  - An edit stamps `engine.dirty.<rule>`.
  - The tick runs the rule once `quiet_seconds` have passed since the latest stamp.
  - The flag is cleared only if no newer edit re-stamped it.
- **Logging (invariant 7):**
  - Records are queued in Script Properties (at most 200; drops are counted).
  - Flushing is best effort, at the end of each execution.
  - After a failure, non-forced flushes back off for 5 minutes.
  - Flush failures are reported as a counter plus the last error in the next
    successful payload, so an outage can't grow the queue on its own.
  - Every caught error is enqueued. The only exception is a failure of the queue
    itself, which goes to `console.error`.
- **Value census:** every run record carries `unknown_values` — enum values that
  matched no stage, per enum, up to 20 each. This feeds M4 Class C detection.
- **Webhooks:** the engine does not send `POST /webhooks/edit` pings in M2. It runs
  `on_edit` rules locally, as SPEC §5 says. How that fits M4's "burst of 10 edits →
  exactly 1 run" (server-side debounce) is an open question for M4.
- **"Today":** script-timezone midnight, like legacy.

### Known limitation (accepted)
- A cell with an explicit white fill reads back the same as a cell with no fill.
  - The engine treats both as "no fill" (legacy cleared fills anyway).

## S1 — Organize on command (SPEC-PATCH-001, with PATCH-002 B.6/B.8/B.9 in force)

### Adapter and ops
- **Reading uses `spreadsheets.get` with grid data, not `values.batchGet`.**
  - `batchGet` can't return fonts, fills or strikethrough.
  - It also returns dates as bare serial numbers, and only the number-format type tells a
    date apart from a number.
  - The read is one call with a field mask. A tab is trimmed to its last row and column
    that hold content, the same as Apps Script `getDataRange`.
- **Writing is one metadata `get` plus one `batchUpdate`.** A `batchUpdate` applies all of
  its requests or none of them (invariant 5). There are no separate `values.update` calls.
- **GridOps come from a diff.** `app/services/ops.py` compares the grid as read with the
  workbook `execute_run` produces.
  - Only rows whose values changed are rewritten, so unchanged rows (held rows included)
    keep their formulas.
  - Row deletes and inserts from move/dedupe are written as value and format rewrites,
    not `deleteDimension`. Only the tracked formats (font, fill, strike) move with the data.
- **Dates:** a date written into a cell also gets a date number format. The format is the
  pattern that column had at read time, or the first date pattern found in the workbook.
  This copies Apps Script `setValues`, which date-formats cells it writes Date objects into.
- **Colours:** a white fill and a black font compare equal to "unset". A theme colour reads
  as unset. Without this, the second run would keep rewriting cells that look identical.
- **Protection:** a locked (consolidate) tab gets a whole-sheet protected range. Its only
  editor is the service account.
- **Dry-run and live share one path.** `prepare_run` computes the plan, the report
  (`summarize_plan`) and the ops once. Dry-run simply never calls `commit_run`.
- **"Today"** is the current date in the spreadsheet's own time zone
  (`properties.timeZone`).

### Safety additions
- **The config's `sheet_id` must equal `--sheet-id`.** A config is never run against a
  different spreadsheet.
- **Stale check:** `commit_run` re-reads the sheet just before writing. If anything changed
  since planning, it writes nothing and raises `StaleGrid`. There is no server-side
  equivalent of the Apps Script lock.
- **Live runs refuse to start without `SNAPSHOT_KEY`.**

### Privacy (PATCH-002)
- **B.6:**
  - `app/core/redaction.py::Redacted` wraps `WriteValues` payloads. Only
    `sheets_adapter._value_requests` calls `reveal()`, and a test enforces that.
  - `Tab`, `Workbook`, `Grid` and `Snapshot` reprs print `<redacted>`.
  - The lint test parses the code with AST. It fails if a cell-value name or `.reveal`
    reaches a log, print, raise, `warnings.append`, or `error=` / `.error =` / `.blocked =`
    site. `type(x)` and `len(x)` are allowed.
  - Fixed one leak: `snapshots._dec_cell` used to put the raw value in its error message.
- **B.8 (partial):** there's no database in S1, so the store is one JSON file per run
  (`LocalSnapshotStore`).
  - Bodies are Fernet-encrypted and carry a `key_id`.
  - The retention purge leaves a tombstone, so loading a purged run raises
    `SnapshotExpired` with a clear message.
  - S2 moves this to the Postgres `snapshots` table and adds the daily purge job.
- **B.9:** in S1 the registry is `TEST_SHEET_ID` plus `ENROLLED_SHEET_IDS` from the env.
  Every adapter call checks it before touching the API. S2 swaps in the `sheets` table.
  The adapter calls only the Sheets API.

### Not reproduced / retired
- Engine.gs, `engine/`, and its parity tests remain in the repo, but no runtime uses them
  (PATCH-001 A.1). They are still part of the test suite.
- Consolidate `presentation` has no implementation now: the title banner, banding, column
  widths and `date_format`. Engine.gs applied it and the Python evaluator ignores it (see
  BLOCKERS.md).

### Findings from the first real dry-run (2026-09-18)
- **Out-of-range date cells.** A date-formatted cell can hold a number too large (or too
  negative) to be a date. It now reads as a plain number instead of crashing.
- **Consolidate now applies the `presentation` title and header colours** (title on the
  merged banner's anchor cell A1, header across the header row).
  - Engine.gs used to do this. Without it, the first live run would have stripped
    legacy's SUMMARY styling.
  - Banding, column widths and fonts aren't tracked formats, so runs never touch them and
    the existing ones survive.
- **Date vs number.** A date and a number with the same serial count as equal in the diff
  (e.g. a number sitting in a date-formatted cell). Rewriting wouldn't change what Sheets
  stores, and without this rule such a cell would be rewritten on every run.
- **Governed tabs.** In the real workbook they are MACHINES, UNITS, RFB  MOD, SPARES and
  BOUGHT-OUT. The owner approved their header hashes in the working config
  (`backend/var/s1_config.json`, gitignored). SUMMARY is rebuilt from all five.
