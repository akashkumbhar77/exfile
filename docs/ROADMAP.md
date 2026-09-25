# ROADMAP.md — the script generator, from here to release

The product (SPEC-PATCH-005): the user uploads a sheet and describes, in plain English, how it
should be organised: sorting, formatting, consolidating, moving, and when that should happen.
They see what it would do, and download an Apps Script that does it. No accounts, no Google access
from us, nothing kept.

Every phase ends with something a user can do that they could not do before. Phases do not
overlap: a phase starts when the previous one's exit criteria pass (CLAUDE.md build order).

Agreed with the owner 2026-09-25. The milestone definitions are SPEC-PATCH-005 F and I; this file
is the working breakdown and is kept current as items close.

## Owner decisions this roadmap rests on (2026-09-25)

1. **Default trigger.** When an instruction does not say *when* to run ("sort by stage, colour
   overdue rows red"), the compiler chooses `debounced` (quiet period 60 s) on edits to the
   governed tabs, and the script's menu always has "Run now". The readback states the chosen
   timing in words, so the default is never silent and the user can override it by saying so.
2. **The paste-header-rows path (PATCH-005 D.1) is deferred until after P5.** With no data rows
   there is no meaningful preview, and the preview is the product.

## P1 — the generator is complete (in progress)

Exit: every supported action has a template, and offline parity is green on the **unmodified**
reference config.

- [x] Sort, format (row + cell, numeric conditions) and consolidate templates, with parity.
- [x] **Trigger wiring.** `installTrigger()` (installable edit + open, run as owner, replaces its
      own earlier triggers and orphans), a menu (Run now, Pause/Resume), on-edit column filters,
      `after` chains, debounced via dirty flag + time trigger, hourly-or-coarser schedules. Each
      event kind is parity-tested against the evaluator's rule selection
      (`tests/test_apps_script_triggers.py`).
- [x] Reference config emits whole; D.4 proven offline.
- [x] **Presentation as intent** (owner ruling 2026-09-20): the evaluator models per-column
      number format (incl. `match_source`), per-column width, and banding on/off + theme on the
      data range. The mock records the calls; parity compares them.
- [x] Templates for validate (dropdowns), dedupe, clear, move, copy. Move, clear and dedupe force
      the backup tab on (PATCH-005 I.7). Each with parity tests.
- [ ] `docs/MOCK-DIVERGENCES.md` kept current (started 2026-09-25). Anything P4 exposes is back-ported into the mock
      as a test case.
- [ ] Owner decisions from the templates: `KEEP_RUNS = 10` for the backup tab, and formulas in
      rewritten rows (DECISIONS "P1: the remaining templates").
- [ ] Close: merge `s7-numeric-conditions` to main.

## P1.5 — read the uploaded sheet

Exit (PATCH-005 I.3): values, fills, font colours, strikethrough and merged cells read into the
evaluator's grid; the reference workbook's two known bad cells (the year-95637 date, the
headerless column) read without crashing.

- [ ] `.xlsx` → `Grid`, fidelity-tested against the reference workbook.
- [ ] Header row / data start detected, or asked for when ambiguous.
- [ ] The grid ends at the last row with content (like `getLastRow`); trailing formatted-but-empty
      rows are not part of it (MOCK-DIVERGENCES #14).
- [ ] Formula cells are detected and reported, so the readback can warn about rewritten rows.
- [ ] Timezone is explicit input, never inferred.

## P2 — end to end on the command line

Exit: `generate --file X.xlsx --instruction "..." --tz Asia/Kolkata` writes the `.gs` and
`config.json`, and prints the readback, clause coverage and preview summary.

- [ ] Profile → mask (B.7) → compile → validate on an uploaded grid, no Google, no storage.
- [ ] Readback plus the mechanical clause-coverage diff (D.2): each clause of the instruction and
      the rule that covers it, or "not covered".
- [ ] Before/after preview from the evaluator, headed with the date and timezone.
- [ ] Schedules read back in words, including that they run within the named hour, not at
      its minute (MOCK-DIVERGENCES #3).
- [ ] Refusals in the user's terms ("every 5 minutes isn't possible; hourly is the finest").
- [ ] Re-uploading `config.json` regenerates without the instruction (D.5).
- [ ] The compiler applies decision 1 (default trigger) and says so in the readback.

## P3 — the web page

Exit: one page does P2's flow, and revising the instruction is fast and obvious.

- [ ] Upload + instruction → readback, clause coverage, preview → revise or download.
- [ ] Sessions in memory only, hard TTL, upload size cap (I.6).
- [ ] Timezone selector defaulting to the browser's zone.
- [ ] Install card (Extensions → Apps Script → paste → run `installTrigger`) and the honest
      limits (G, plus I.4 preview fidelity), never softened.

## P4 — live proof (release gate)

Exit (PATCH-004 D.3): the generated script, installed by hand in a real test spreadsheet,
reproduces the evaluator's preview exactly.

- [ ] Read back through the fenced, test-only Google path (I.1), including number formats,
      column widths and banded ranges, so presentation is compared live, as it is offline.
- [ ] Every difference becomes a mock test case and an entry in MOCK-DIVERGENCES.md.

## P5 — the gauntlet

Exit: 8–10 structurally different `.xlsx` files, each with its owner's own sentence, scored for
schema coverage, compile success and intent match, written up in `docs/GAUNTLET.md`.

## After P5

- The paste-header-rows path (decision 2), if the gauntlet shows demand for it.
- Whatever the gauntlet findings drive into the next spec patch.
