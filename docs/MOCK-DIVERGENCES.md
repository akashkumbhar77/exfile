# MOCK-DIVERGENCES.md — where the Apps Script mock is not Apps Script

The offline parity proof runs generated scripts in `engine/test/gas_mock.js`. The mock is
anchored to the legacy golden files (`engine/test/engine.test.js`), but it is still a model, and
every place it differs from real Apps Script is a place the offline proof can be wrong. This list
exists so those places are known rather than discovered.

**Rule (owner, 2026-09-20):** anything P4's live run exposes is back-ported into the mock as a
test case, and recorded here with the test's name. A live finding that is not in the mock can
happen again unnoticed.

Status: **open** = the mock differs and the generated code works around it or does not depend on
it; **to verify at P4** = believed correct, not yet confirmed live; **back-ported** = closed by a
mock change plus a test.

| # | Area | Mock | Real Apps Script | Consequence for generated code | Status |
|---|------|------|------------------|--------------------------------|--------|
| 1 | Range singular accessors | `getFontColor`, `getBackground`, `getValue` are missing; only the plural forms exist | Both exist | A template using a singular accessor crashes offline but works live. Templates use the plural forms only (found while building consolidate). | open |
| 2 | `Utilities.formatDate` | Ignores the timezone argument and formats in the harness process zone (`TZ`, default Asia/Kolkata) | Formats in the zone passed | The schedule check (`dueSchedules_`) formats "now" in the script's zone. Offline, script zone = process zone, so the difference never shows. A test with a script zone unlike the process zone would pass wrongly. | open |
| 3 | Time-driven trigger cadence | `tick` fires every CLOCK trigger at once, ignoring `everyMinutes` / `everyHours` | One-minute triggers fire roughly each minute; hourly ones at a Google-chosen minute within the hour, with jitter | The script checks elapsed time (debounce) and the current hour (schedule) itself, so offline tests exercise those checks rather than the cadence. A schedule runs *within* its hour, not at its minute; that is stated in the script and must be stated in the readback. | open |
| 4 | `LockService` | `tryLock` returns at once (`__lockBusy` flag), never waits | `tryLock(30000)` waits up to 30 s for the lock | The "another run was still going" path is not exercised offline. | to verify at P4 |
| 5 | Edit events | Only harness edits fire `ON_EDIT`, always a single cell | Pastes span many cells and columns; edits by scripts, the API, and some structural operations (inserting rows) do not fire `onEdit` | Multi-column edits are handled (every column in `e.range`) but tested only for single cells. Row insertion does not trigger a run live: Run now covers it. | to verify at P4 |
| 6 | `SpreadsheetApp.getUi()` | Always works | Throws "Cannot call SpreadsheetApp.getUi() from this context" in some contexts (time-driven triggers, possibly editor runs) | `installTrigger` builds the menu inside a try and tells the owner to reload if it could not. `getUi` is never called from a trigger. | to verify at P4 |
| 7 | `globalThis` | Node's global | V8 runtime supports it (ES2020) | Used to detect orphan triggers whose function no longer exists. | to verify at P4 |
| 8 | Protection | `protected: true/false` on the tab | A protection with an editor list; the owner stays an editor, and "warning only" is a separate mode | `lockTab_` is checked offline only for presence. That the owner can still run the script against a locked target is unproven. | to verify at P4 |
| 9 | Quotas and limits | None, except the 9 KB script-property limit | 6-minute execution limit, a daily trigger-runtime quota, 20 triggers per user per script | Large sheets could exceed 6 minutes; the one-minute debounce trigger uses a small, steady share of the daily quota. Not modelled. | open |
| 10 | `Range.getNumberFormat` on an unformatted cell | Returns `General` | Believed to return `General` | The evaluator treats an absent format as `General`, so `match_source` copies `General` from a source nobody formatted. If Sheets reports something else, both the evaluator default and the mock change together. | to verify at P4 |
| 11 | `applyRowBanding` over an existing banding | Adds a second banding silently | Throws ("You cannot add alternating colors to a range that already has alternating colors") | The consolidate write removes every banding on the target before rebuilding it; `test_rebuilding_a_banded_summary_leaves_exactly_one_banding` holds that. The mock was not changed on belief alone: the P4 rule applies. | open |
| 12 | `Sheet.clear()` and bandings | Keeps bandings (clears values and formats) | Unconfirmed whether `clear()` removes bandings | Irrelevant to the script, which removes bandings explicitly first. | to verify at P4 |
| 13 | Rows inserted by `insertRowsBefore` | Blank: no formatting, no dropdowns | Believed to take formatting (and possibly dropdowns) from a neighbouring row | The script clears formatting and dropdowns on inserted rows explicitly, so the result matches the evaluator either way. A mutant that skips the clearing survives offline for exactly this reason. | to verify at P4 |
| 14 | Formatted rows below the data | None: rows past the last value are default | A sheet can have formatted, empty rows below its data; rows removed from the data pull them up | The evaluator treats everything past the grid as default. The P1.5 reader must end the grid at the last row with content, and the parity comparator now requires rows below the grid to be untouched. | open |
| 15 | Formulas | Modelled since 2026-09-25: a cell may hold a formula; writing a value or clearing the content replaces it | The same (documented Sheets behaviour); `getValues` returns results | Clear empties only its cells; sort rewrites only rows that move. A moved row's formula becomes its value, which is a stated limit. Formula results are not recomputed in the mock, so a formula depending on a moved cell is not checked. | to verify at P4 |

## Emitter bugs found by trigger wiring (not mock divergences, recorded for the P4 reader)

- **Consolidate read its sources in alphabetical order**, not sheet order. The evaluator visits
  tabs in workbook order. The reference workbook's tabs happen to be alphabetical, so parity
  never showed it. Fixed: runs now read governed tabs in the order they appear.
- **The script wrote rule by rule**, so a rule that failed or tripped the row guard left earlier
  rules' writes in place. The evaluator aborts the whole run. Fixed: every rule is planned in
  memory and nothing is written unless all of them succeed
  (`test_a_blocked_rule_leaves_the_whole_sheet_untouched`). A write that fails midway through the
  final write phase can still leave a partial result. Apps Script has no transactions; that is a
  limit to state, not something the mock can prove away.
