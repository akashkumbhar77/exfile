# SPEC-PATCH-005.md — Pivot: script generator only

Read after CLAUDE.md, SPEC.md, PATCH-001/002/003/004. Where this
conflicts with any of them, THIS PATCH WINS. It narrows the product to
one feature and parks the rest. Nothing here is deleted; everything
parked stays in git, tagged, and can be revived by a later patch.

## A. The product, in one sentence

A person describes in plain English how a spreadsheet should organise
itself, sees a real before/after preview, and downloads an Apps Script
file they paste into their own sheet. No account, no sharing, no
service account, no OAuth, nothing of theirs stored that they did not
upload.

Anything that does not serve that sentence is out of scope until a
later patch says otherwise.

## B. Role changes (the important part)

1. **The Python evaluator is no longer a runtime. It is the ORACLE.**
   Two jobs, both kept and both tested:
   - it renders the before/after preview shown before download
   - it is the reference the generated script is proven against (D of
     PATCH-004)
   Deleting or weakening it removes the only proof the emitter is
   correct. It is load-bearing and must not be parked.
2. **The apps_script emitter is the only delivery target.** PATCH-004
   stands in full, except that its E.4 (one tier per sheet) is moot:
   there is no server tier to conflict with.
3. **ConfigSpec remains the single definition of rule semantics.** The
   compile path (instruction -> config -> validate -> describe) is
   unchanged and stays the heart of the product.

## C. Parked (do not build, do not maintain, do not delete)

Fleet watcher and Drive changes feed · polling and debounce · server-side
execution and the sheets write path · snapshots, undo, resume · drift
pause and repair agent · weekly audit · anomaly checks · registry of
enrolled sheets · service-account access and all Google auth ·
Approvals/Sheet-detail/Fleet pages · RQ/Redis (compile runs synchronously
or in-process) · org/tier tables beyond what the new flow needs.

Method: tag the current main as `v0-managed-tier` before the first
pivot commit, record in DECISIONS.md what the tag contains and why it
was parked, and remove parked code from the active build in one clearly
labelled commit. Tests for parked code are removed with it, not left
failing. Keep the sheets adapter's READ path only if the xlsx reader
does not already cover the preview need; otherwise park it too.

## D. The flow to build

1. **Input**: the user uploads an .xlsx (or pastes header rows for a
   lighter path) and types the instruction. No sign-in.
2. **Compile**: profile the uploaded grid, compile the instruction to a
   config, validate, and produce:
   - the plain-English readback (`describe.py`)
   - the clause-coverage notes (mechanical diff, PATCH-004 follow-up)
   - a before/after preview computed by the evaluator on the uploaded
     grid: rows reordered, cells recoloured, rows moved, per tab
3. **Review**: the user reads the readback, sees the preview, and may
   revise the instruction and recompile. Revision is the main loop of
   this product and must be fast and obvious in the UI.
4. **Download**: the generated `.gs` plus the 4-step install card
   (Extensions -> Apps Script -> paste -> run `installTrigger`), and a
   plain statement of what the script does not do.
5. **Return visit**: the config JSON downloads alongside the script so a
   user can re-upload it later to regenerate or amend without retyping
   the instruction. No account required for this to work.

## E. Privacy under the pivot (simpler, and now a selling point)

1. Uploaded files are processed and deleted. Retain only for the length
   of the session, with a hard TTL; never persist cell values.
2. B.6 (no cell values in logs, errors, metrics) and B.7 (masking before
   anything reaches the model) stand unchanged and now cover the upload
   path.
3. Generated scripts remain offline artifacts: no telemetry, no
   callbacks, no embedded URLs or credentials (PATCH-004 A.4).
4. The one-line claim the product may make, because it is now literally
   true: we never connect to your spreadsheets, and we keep nothing.
   Do not weaken it with a feature that contradicts it.

## F. Milestones (replace the S-series from here)

- **P1** Emitter completes: all supported actions have templates; the
  offline parity harness (Node + faked Sheets API, validated against the
  legacy golden files) is green on the reference workbook config.
- **P2** Upload -> compile -> readback + preview -> download, end to end,
  as a CLI first: `cli.py generate --file X.xlsx --instruction "..."`
  writes the `.gs` and prints the readback and preview summary.
- **P3** Web UI for that same flow: one page, upload + instruction ->
  readback + preview -> revise or download. Reuse PATCH-003's principles
  (templated readback, no cell values persisted, values on screen only).
- **P4** Live proof: the generated script, installed by hand in a real
  test spreadsheet, reproduces the evaluator's preview exactly
  (PATCH-004 D.3). This is the release gate; no launch before it passes.
- **P5** Gauntlet, rerun for this product: 8-10 structurally different
  xlsx files, each with its owner's own sentence. Score schema coverage,
  compile success, intent match. Findings go to docs/GAUNTLET.md and
  drive the next patch.

## G. Honest limits to state in the UI (never soften)

The script cannot be updated remotely once installed; changing the
automation means regenerating and re-pasting. There is no drift repair:
if a header is renamed the script pauses and tells the owner, and the
owner must regenerate. There is no undo beyond the optional backup tab.
Nothing is monitored; we do not know if it stopped.

## H. What this pivot gives up (recorded, not hidden)

Recurring managed value, fleet visibility, cross-file rules, and the
managed tier's upgrade story. If those are wanted later, `v0-managed-tier`
is the starting point and the config schema is unchanged, so the parked
work still fits.
