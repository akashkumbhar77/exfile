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

## S2: React to edits and undo (SPEC-PATCH-001 S2, SPEC-PATCH-002 A plus B.6/B.8/B.9)

### Registry and schema
- **Tables:** migration `0001` creates orgs, sheets, configs, runs, snapshots, events,
  watch_state, and flags (schema only).
  - Every table has `org_id`. On `orgs` it equals `id`, so every table can be scoped the
    same way.
  - The `profiles` table waits for S3.
  - The sheets→configs foreign key cycle is added after both tables exist. The migration
    was tested for upgrade, downgrade, re-upgrade and a clean `alembic check` on Postgres 16.
- **Status values:**
  - Sheet: PENDING, ACTIVE, PAUSED_DRIFT or PAUSED.
  - Config: PENDING_APPROVAL, ACTIVE, SUPERSEDED or REJECTED.
- **Enrollment is two steps (B.9 vs. invariant 11).** A dry-run has to read the sheet, but
  B.9 forbids opening unregistered files. So `register` adds the sheet as **PENDING**, runs
  the dry-run, then asks the owner to approve. Only ACTIVE sheets are watched or run.
- **`runs` rows:** one per rule for real runs (from the executor). A single rule-less row
  covers NOOP, STALE, ERROR, PAUSED_DRIFT and undo.
  - An OK run whose diff is empty is logged as one NOOP row with `rows_affected=0`
    (PATCH-002 A.2), not as per-rule rows with evaluation counts.

### Detection and debounce
- **One feed for the fleet.** `changes.list` over the service account's corpus, with
  `includeItemsFromAllDrives`. The page token is persisted in the same transaction as the
  debounce touches, so delivery is at-least-once and never lost. The first poll only stores
  the start token.
- **Debounce is a Redis sorted set of due times.**
  - `touch` re-arms a sheet by setting its due time to now + 30s.
  - Once per poll, touches happen *before* the dispatch step, so a burst that spans a poll
    boundary still re-arms instead of dispatching early.
  - Only the watcher touches and dispatches, which avoids races. RQ executes the jobs.
  - RQ's own scheduler isn't used, which keeps Windows (SimpleWorker, no fork) supported.
- **A `change` run event runs every edit-driven rule** (on_edit and debounced) over all its
  tabs. The Drive feed says a file changed, not which cells, and the diff keeps the writes
  minimal.
- **Fingerprint gate (A.2).** Each governed tab (including consolidate targets) gets a
  SHA-256 hash of its values, stored as `sheets.last_fingerprint`.
  - The hash records what the sheet stores: text, number or boolean, with dates as serial
    numbers. So date-formatted numbers and `1` vs `1.0` don't cause spurious re-runs.
  - After a committed run, the stored fingerprint is that of the *projected* post-run state.
- **Self-write suppression (A.3).** After each write, `files.get(modifiedTime)` on the
  registered file becomes `self_write_watermark`, and the watcher drops changes at or
  before it.
  - Known gap: an edit made within about a second after our write can be swallowed until
    the user's next edit. Changes *after* the watermark always trigger.
- **Per-sheet Redis lock** (`lock:run:{id}`). A job that finds a run in progress re-arms
  the debounce instead of running twice. A STALE run (the user edited mid-run) also
  re-arms.

### Undo, resume, purge
- **Undo** restores snapshots newest-first with the shared diff and write path.
  - It refuses if the run's tabs changed after it, judged by the `post_fingerprint` stored
    in the `run.committed` event. `--force` overrides.
  - The undo takes its own snapshot first, so it can be undone too.
  - The sheet's status is unchanged: the next human edit re-applies the rules. Pausing is a
    separate decision for the owner (a `pause` command isn't built; SPEC mentions it for the
    dashboard).
- **Resume** re-checks the live headers against the approved config. It refuses while the
  sheet is still drifted; on success it sets ACTIVE and schedules an immediate run.
- **Snapshot purge (B.8).**
  - The watcher runs it once every 24h; `cli.py purge-snapshots` runs it now.
  - Bodies are set to NULL and the rows stay, so undo reports "older than the N-day
    retention".
  - Retention is `orgs.snapshot_retention_days`, 30 by default.
- **Where snapshots live:** registered sheets use the Postgres `DbSnapshotStore`. The S1
  file-config path (`run --config`) keeps the local encrypted store.

### Privacy
- **B.6 error text:** `safe_error` persists our own error messages. For Google HttpErrors it
  keeps the type and status with quoted fragments stripped (Google can echo rejected
  input); anything else keeps only the type name.
  - Worker logs record traceback frames but not the exception message.
  - A test scans every runs and events row for cell values.
- **B.9:**
  - The adapter and the Drive `files.get` both check `DbRegistry` before any call.
  - Drive scope is `drive.metadata.readonly`.
  - An AST test forbids `files().list` and `q=` searches anywhere in `app/`.

### Infrastructure
- **Tests use an embedded Postgres 16** (`pgserver`, migrated by Alembic), fakeredis with
  real RQ jobs, and fake Sheets/Drive services that bump `modifiedTime` on every commit.
- **`docker-compose.yml`** runs postgres, redis, migrate, watcher and worker. The backend
  image never contains `.env` or keys (`.dockerignore`); the key is mounted read-only.

### S2 live findings (2026-09-18, real workbook, two sheets watched)
- **The changes feed does see sheets shared with the service account.** One user edit
  appeared in the next poll and produced exactly one run; the other sheet was untouched.
- **PATCH-002 A.3's watermark doesn't work as written.** An immediate `files.get(modifiedTime)`
  after a Sheets `batchUpdate` returns the *previous* time. Drive only reports our write's
  time (which does equal the write moment) several minutes later. Waiting 20s didn't help.
  - Drive also **re-emits** the change record for the same write a few minutes after the
    first. Before this fix, one write caused two harmless NOOP runs.
- **Resolution: recognize our own writes by author, with the watermark as a second layer.**
  - The feed request includes `file.lastModifyingUser.me`, and the watcher drops changes
    made by the service account itself. No email address is stored or logged.
  - This is safe both ways:
    - A human edit after our write makes the human the last modifier, so it triggers a run.
    - A human edit during a run is caught by the stale check before the write.
  - The `modifiedTime` watermark (an immediate get, as A.3 specifies) still applies.
  - The fingerprint gate stays the last line of defence: a self-write that slips through
    becomes a logged NOOP with no writes.
  - This departs from A.3's literal mechanism but keeps its intent ("guards the
    bot-triggers-bot loop … just as actor-filtering did for webhooks"). **Owner to confirm.**

## S3: Plain English in front (SPEC-PATCH-001 S3, SPEC §6, B.7)

### Provider (owner decision, 2026-09-18)
- **The agent uses OpenAI**, which overrides CLAUDE.md's Anthropic lock. It calls plain
  chat-completions with tools; there is no agent framework.
- **Models are settings:** `LLM_PRIMARY_MODEL` (default `gpt-4.1-mini`) and
  `LLM_ESCALATION_MODEL` (default `gpt-4.1`). They're checked against the key's
  `models.list` before the first call, and a missing model fails early with a clear message.
- **Caching:** OpenAI caches long identical prompt prefixes automatically. The system prompt
  (instructions, JSON Schema, worked example) and the profile go first as a stable prefix.
  `llm_calls.cached_tokens` records the hits.

### Agent loop
- **Tools:** `get_profile`, `sample_rows` (masked), `propose_config`. SPEC's separate
  `run_dry_run` is folded into `propose_config`: a proposal that validates is dry-run on the
  same grid read at once, and the counts are returned.
- **The server fills** `sheet_id`, `org_id` and `config_version`, plus the real `schema_hashes`
  for every tab the model lists (the model writes `"auto"`). A tab that doesn't exist is
  reported back as an error.
- **Stopping and escalating.** The first proposal that validates *and* has a dry-run status
  of OK is stored as PENDING_APPROVAL, and the loop STOPS. If it fails instead:
  - Each rejected proposal counts as one failed attempt, as does a reply without a proposal,
    or running out of turns (8 per attempt).
  - The schedule is 2 failed attempts on the primary model, then 1 on the escalation model
    (continuing the same conversation, so it sees the earlier validator errors).
  - After that, the `onboarding.needs_human` event is written (the human ticket) and nothing
    is stored.
- **Declines.** A model reply starting with `CANNOT:` ends onboarding with that
  human-readable reason. This is how a nonsense instruction fails without an ACTIVE config.
- **Enrollment and approval.** `enroll` first adds the sheet as PENDING (B.9), stores a
  versioned profile, runs the agent, shows the dry-run, then asks for y/n approval
  (invariant 11).
- **Worked example.** The prompt's example is a support-tickets workbook, not the SPEC §1
  example, which is nearly the reference config itself and would make the S3 exit criterion
  trivial. A test checks that the example passes the validator.

### Masking and profiles (B.7)
- **What passes through unmasked:** status-like columns (the header matches STATUS, STAGE,
  STATE, FREEZE, TYPE, CATEGORY, PRIORITY…, ends in `?`, or every value is yes/no-like). The
  labels of these columns are structure.
  - This is never inferred from low cardinality alone, so a column of a few repeated customer
    names is always masked.
  - Headers that look like company, email or phone columns are never treated as status-like.
- **What's masked:**
  - Text by column kind: `Company_X` for customer/client/vendor columns, `Person_X` for
    name/contact columns, `Text_X` otherwise. The same original gets the same placeholder
    within one call.
  - Values containing digits become random characters of the same shape, and never the
    input itself.
  - Emails and phones keep their shape; emails end in `.example`.
  - Integers keep their digit count; floats get a random value within 0.5–1.5× the original,
    with the same number of decimals.
  - Dates shift by one random offset per call (±30–400 days), so their order is kept.
- **Profiles** hold tab/header/type/fill-rate/distinct counts plus status-label distributions,
  and nothing else. Tests assert that no free-text value appears in profiles or in anything
  sent to the LLM.
- **`llm_calls`** stores metadata only: model, attempt, turn, tokens (including cached),
  latency, tool names and status. It never stores prompt or reply text.

### S3 live findings (2026-09-18, sheet 2, same instruction each run)
- **`gpt-4.1-mini` → `gpt-4.1`: 1 usable result in 7 runs.** They repeated the same schema
  mistakes after feedback:
  - nested `date` objects
  - governing or targeting the SUMMARY tab
  - `contains:STATUS`, which also matched CONTROL PANEL STATUS and merged the two columns
- **`gpt-5-mini` → `gpt-5.1`: 4 of 4 runs produced a config.**
  - Each run first hit the new column-merge check, then fixed its proposal.
  - With prompt v3, all 5 category tabs and all SUMMARY data, headers and colours match the
    hand-written config exactly.
  - The only remaining difference is the SUMMARY title banner (below).
  - Cost: about 48k input tokens (30–46k of them cached) and 5–8k output tokens per run.
- **Nonsense instruction** (emails, translation, courier) → FAILED with a readable
  explanation, no config, a human ticket, and the live config unchanged.
- **Agent-side checks added.** They run before the dry-run; the validator contract is
  unchanged.
  1. A pattern in `canonical_headers` that matches 2+ different headers on one governed tab
     is rejected, naming the headers. This prevents silent column merging.
  2. The server drops consolidate targets from `schema_hashes`, which it owns anyway, and
     tells the model.
  3. A config sent as a JSON string is parsed.
  4. Validator errors carry plain-language hints for common mistakes.
  5. The dry-run reply lists each consolidate target's resulting headers next to its
     existing headers.
- **Prompt v3 adds** flat condition syntax, "targets only via sort_like/format_like",
  fragment stage matching, reuse of existing header spellings, and "later format rules win".
  All of it is generic guidance, with nothing specific to the owner's workbook.
- **The SUMMARY title banner can't be inferred.** It's cell text in row 1, and B.7 only lets
  headers, tab names and status labels reach the model, so a compiled config has no
  `presentation.title`. Owner decision pending.
- **`OPEN_AI_API_KEY` is accepted** as an alias of `OPENAI_API_KEY` (the owner's `.env` uses it).

### Skills and defaults (owner request, 2026-09-18)
- **Defaults: `gpt-5-mini` → `gpt-5.1`, with skills on** (`LLM_USE_SKILLS=true`).
- **Skills are on-demand reference docs** (`app/agent/skills/*.md`), loaded with a `load_skill`
  tool. It's a plain tool, so the "no agent framework" rule still holds.
  - The core prompt (v4, about 0.8k tokens) holds the workflow, a config skeleton, and a
    one-line catalogue.
  - Each skill returns its guidance plus exactly its slice of the JSON Schema (`schema_defs`
    in its front matter; a test checks they exist).
  - The six skills are config-basics, triggers, stages-and-sort, conditions-and-format,
    consolidate, and move-copy-cleanup.
- **Correction, stated plainly: earlier runs had answer hints.** The v2/v3 prompt examples
  used the reference workbook's own specifics (DISPATCH DATE, FREEZE?, "in-process red,
  overdue black on pink"), which helped the earlier gpt-5 runs.
  - All prompts and skills now use a support-tickets example domain instead.
  - A test fails if any prompt or skill contains the reference solution's terms.
  - The comparison below was run after this fix.
- **A doc bug fixed along the way.** The docs said undeclared columns keep their header text;
  in fact the validator rejects any rule reference to an undeclared column (SPEC §1). Every
  skills run was losing one attempt to this.
- **Live comparison** (sheet 2, same instruction, gpt-5-mini, answer-free prompts, three
  runs each):

  | variant | proposed | failed attempts | input tokens/run | exact except title |
  |---|---|---|---|---|
  | full prompt v3 | 3/3 | 1 each | ~48.5k | 2/3 (1 rule-order colour error) |
  | skills v4 | 3/3 | 0 | ~18k (−63%) | 2/3 (1 renamed summary column) |
  | skills v4 + rename review | 3/3 | 0, 0, 1 | 18k / 18k / 31k | **3/3** |
- **One-time review of renames on an existing consolidate target.** A proposal that would
  rename an existing summary tab's columns is sent back once, listing the lost and new
  columns. Proposing the same headers again is accepted, so an intended rename still works.
  This catches silent renames without blocking deliberate ones.

### Model decision (owner, logged 2026-09-18)
| role | model | why |
|---|---|---|
| primary (`LLM_PRIMARY_MODEL`) | **gpt-5-mini** | Live on the real workbook, with answer-free prompts and skills: 6 of 6 runs proposed a valid config, and 3 of 3 with the rename review matched the hand-written config. `gpt-4.1-mini` → `gpt-4.1` produced a usable config in 1 of 7 runs. |
| escalation (`LLM_ESCALATION_MODEL`) | **gpt-5.1** | Stronger model from the same family. It's only used after 2 failed proposals on the primary, which didn't happen in the final runs. |
| provider | OpenAI | Owner override of CLAUDE.md's Anthropic lock (see "S3 → Provider"). |
- Skills stay on (`LLM_USE_SKILLS=true`): about 18k input tokens per run versus about 48.5k with
  the full prompt, and 0 failed attempts.
- To change the models, edit `.env`. `enroll` checks the models against the account's model
  list before its first call.

### S3 close-out (2026-09-18)
- **Title, decision (c) as defined in PATCH-003 B1:** the owner supplies the title for
  consolidated tabs at approval (`approve_config(summary_title=…)`, `--summary-title`, and
  later the Approvals page).
  - It's written into each consolidate rule's `presentation.title` and stored on
    `configs.summary_title`.
  - The onboarding model never reads it. This differs from an earlier proposal in this log to
    have the engine keep the existing title; PATCH-003 is the governing spec.
- **Every config now records:**
  - `decision_reason`, `rejected_by` and `rejected_at`. A reason is required to reject.
  - `governed_headers`, a header-text snapshot (structure only) used for the drift
    "what changed" view.
  - `source`: onboarding session, model and prompt version, or `cli`.

  Added by migration 0003.
- **Configs v2–v19 on sheet 2 were rejected** with the reason "benchmark artifact: S3 live
  model/prompt trials".
- **Final enrolment of sheet 2** through `cli.py enroll --leave-pending`: config v20 (id 21),
  gpt-5-mini, first attempt, left PENDING_APPROVAL for the Approvals page.
  - Proposed as is, it differs from the hand-written config only in the SUMMARY banner.
  - With the owner title applied, it's identical on every tab and makes 0 changes to the
    live sheet.

## S5 pre-frontend: API for SPEC-PATCH-003

- **The HTTP API is new.** Before this, only the CLI existed. It's FastAPI under `/api/v1`
  (SPEC §3 paths), plus the PATCH-003 section-B additions. `docs/API-FIELDS.md` maps every page
  field to its endpoint and lists the gaps closed.
- **Auth (PATCH-003 C):** a static `API_TOKEN` compared in constant time. With no token
  configured the API answers 503; there's no open mode.
  - `actor` in approve and reject bodies is a free-text name recorded on the config. There are
    no users.
- **One error envelope:** `{"error": {code, message, request_id, details?}}` for every failure.
  - Unknown exceptions return "internal error" plus the request id, and are logged with their
    type and traceback frames only. The frames are source lines, never runtime values; the
    exception message isn't logged.
  - Validation errors list field locations and reasons, never the submitted input.
  - Tests check that a runtime cell value inside an exception reaches neither the response
    nor the logs (B.6 on the envelope path).
- **Preview (PATCH-003 A.3, B.6):** `GET /configs/{id}/dry-run/preview` is computed on demand
  from the same plan the executor uses, returned, and never persisted or logged. A test checks
  that row counts in runs, events, llm_calls, snapshots and configs don't change.
- **The access check registers first.** `POST /sheets/access-check` adds the sheet as PENDING
  and then reads metadata only, so B.9 ("open only registered file ids") holds literally. A
  failed check leaves a harmless PENDING row.
- **Enrolment is an RQ job** on the `onboarding` queue; the worker now listens on `runs` and
  `onboarding`. Progress is the job's meta state (profiling, compiling, validating,
  dry_running, then proposed or failed), fed by a progress callback in `onboard()`.
- **Flags are derived and read-only:** drift pause and onboarding hand-off. The flags table
  stays schema-only (PATCH-001 B).
- **Undo availability** comes from the snapshot rows: none (the run changed nothing), purged
  or past retention (with the retention days in the reason), or already undone.
- **Time:** views use real time for ages and expiry (rows are stamped by Postgres `now()`).
  The injectable clock is used only for planning ("today") and the debouncer.
- **CORS** allows `CORS_ORIGINS` (default `http://localhost:5173`): methods GET/POST, headers
  Authorization/Content-Type, no credentials. In development the Vite server proxies `/api` to
  :8000 (`frontend/vite.config.ts`).
- **`describe.py` (PATCH-003 A.2)** is deterministic templates per action, trigger and
  condition. Colours are named from a fixed palette with their hex. A golden-text test covers
  the reference config (`tests/fixtures/reference_readback.txt`), plus one test per action.

### S5 styling (PATCH-003 A.5): **CSS Modules**
- **Why CSS Modules:**
  - They're built into Vite: no extra dependency, PostCSS config or class-name build step.
  - Styles are scoped per component, and the choice fits the "one lightweight approach"
    constraint.
  - A small dashboard (four pages, tables, dialogs, status chips) doesn't need a utility
    framework's design system.
- **Conventions:**
  - one `*.module.css` next to each component
  - design tokens (colours, spacing, font sizes, the status-chip palette) as CSS custom
    properties in a single `src/styles/tokens.css`
  - `camelCaseOnly` class names
- **Not used:** Tailwind (it adds a build pipeline and utility classes throughout the markup),
  and component libraries beyond headless primitives (per PATCH-003).

### Addendum to decision (c), the engine title fallback (owner request, 2026-09-18)
- **When a consolidate rule sets no `presentation.title`** and the target tab already exists
  with text in A1 (the anchor of a merged banner), the rebuilt tab keeps that title text and
  A1's style exactly as they are.
  - A brand-new target gets no banner.
  - An owner-supplied title (decision (c), at approval) always wins.
- **Only the engine reads the existing banner, while rebuilding.** Nothing is sent to the
  model, so B.7 is unaffected.
- **The readback says so:** "Keeps the tab's current title banner, if it has one."
- **Test:** `test_existing_title_is_kept_when_config_sets_none` (keep, none-when-new, owner
  wins, and rebuilding twice is a no-op).
- **Live check:** config v20 on sheet 2, without an owner title, now makes 0 changes (before
  this it rewrote the banner).

### Pre-merge hardening of s5-api (2026-09-18)
- **The route-table auth test is built from the app's OpenAPI document.** FastAPI 0.141 nests
  included routers, so `app.routes` no longer lists them. Every `/api/v1` path and method
  except `/health` must answer 401 `unauthorized` to no header, a wrong token, and a bare
  token without `Bearer`. It covers 20 routes today, and new ones are picked up automatically.
- **B.9 at the API layer:** a single guard per id type (`registered_sheet`,
  `registered_config`) returns 404 unless the id is in this org's registry.
  - **This closed a real gap:** approve and reject used to pass the raw `config_id` to the
    registry with no org check.
  - A table-driven test covers all 13 `{sheet_id}`/`{config_id}` routes for both a missing id
    and another org's sheet and config: each must be 404 and make no Google call, and the
    other org's config must stay PENDING.
  - A mutation check confirmed the test fails if the approve guard is removed.
- **The stray `C:\AKASHK~1\` folder** was created by a mistaken tool call, not by project
  code.
  - The session's own `Write` call passed a truncated scratchpad path
    (`C:\AKASHK~1\placeholder.txt`, with `Users\` missing).
  - No code, test, script or config in the repo builds that path (checked with a grep of the
    tracked files).
  - The full test suite, CLI commands, the API, the worker and the watcher all ran without
    recreating it.
  - Nothing in the codebase needed a fix; the folder was removed at the time.
