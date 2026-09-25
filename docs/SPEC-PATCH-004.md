# SPEC-PATCH-004.md — Apps Script emitter (S7)

Read after CLAUDE.md, SPEC.md, PATCH-001/002/003. Where this conflicts,
THIS PATCH WINS. Adds a second compilation target. Does NOT change the
server runtime, the config schema's authority, or any privacy invariant.

## A. Framing (read before writing code)

1. **The config remains the single source of truth for rule semantics.**
   The CLAUDE.md invariant "one server-side engine; rule semantics exist in
   exactly one code path" is amended to: *rule semantics are DEFINED once
   in ConfigSpec; execution targets are EMITTERS compiled from it, and no
   emitter may define semantics the schema does not express.*
2. There are now two targets:
   - `server`: the existing Python evaluator + adapters (managed tier)
   - `apps_script`: a generated `.gs` file the owner installs (self-serve tier)
3. An emitter NEVER invents behavior. If a rule cannot be expressed in a
   target, the emitter refuses and names the rule and the reason. Silent
   partial emission is a build failure, not a warning.
4. Generated scripts are **offline artifacts**. They MUST NOT phone home,
   send telemetry, call our API, or embed credentials, tokens or URLs.
   Once delivered, we are blind to them by design. This is the tier's
   privacy guarantee, and the product story depends on it.
5. This is NOT a revival of the deleted Engine.gs. Engine.gs was a generic
   interpreter reading config at runtime. Emitted scripts are specialized,
   readable code compiled from config (see C.1). Do not restore Engine.gs
   from git history.

## B. Target capability matrix (implement as data, not prose)

`app/emitters/capabilities.py` declares, per action and trigger, whether
the apps_script target supports it. Emission validates the config against
it and returns a structured refusal list (json-pointer + reason), the same
shape as ConfigSpec validation errors.

Supported: sort (incl. stage enums) · format (row + cell rules, font
color, background, strikethrough) · consolidate (single file, cross-tab,
header canonicalization, derived columns, lock) · validate (dropdowns) ·
dedupe · clear · triggers on_edit / on_open / after / debounced (via
dirty flag + time trigger) · `!hold` exemption · max_rows guard ·
header-hash pre-flight self-pause.

Not supported (must refuse with a readable reason): any rule sourcing or
targeting a DIFFERENT spreadsheet file · snapshots/undo (see D.5 for the
limited backup-tab option) · drift repair · central audit · anything that
needs server state or LLM calls.

## C. Generated script requirements

1. **Readable, specialized code** assembled from per-action template
   fragments in `app/emitters/apps_script/templates/`, NOT a generic
   interpreter with an embedded JSON blob. The owner must be able to read
   and hand-edit the result. Each fragment gets unit tests on its emitted
   text.
2. Header comment block carries: product name, generator version, config
   version, generation timestamp, governed tabs, and a one-line
   plain-English summary per rule. Reuse `describe.py` output, the same
   readback the Approvals page shows, so the script and the UI never
   disagree.
3. The **full config JSON is embedded as a trailing comment block only**,
   for traceability and regeneration. It is never parsed at runtime.
4. Self-contained: no libraries, no external fetches, no `UrlFetchApp`.
5. Includes `installTrigger()` (installable onEdit + onOpen, run as owner,
   removes its own earlier triggers before re-creating), an `onOpen()` menu
   with "Run now" and "Pause/Resume", and the header-hash pre-flight that
   pauses and toasts the owner on mismatch instead of running rules
   against a changed sheet (invariant parity with the server target).
6. Batched range operations only: `setValues` / `setFontColors` /
   `setFontLines` / `setBackgrounds` arrays, never per-cell loops.
   Debounced consolidate uses a dirty flag + time trigger, never a full
   rebuild on every edit. (The legacy script's per-edit summary rebuild is
   a known flaw; do not copy it.)
7. No silent `catch {}`. Errors surface as a toast naming the rule.
8. "Today" for date conditions uses the spreadsheet's time zone
   (`Session.getScriptTimeZone()` / spreadsheet setting), matching the
   server target's behavior.
9. Title behavior matches decision (c) + addendum: an owner-supplied title
   is emitted; with no title, consolidate keeps the target tab's existing
   title row.

## D. Parity (non-negotiable; this is what makes two emitters safe)

1. A CI parity harness runs every fixture config through BOTH targets and
   asserts identical resulting grids (values + formats).
2. Server side: the existing Python evaluator against the grid fixtures.
3. Apps Script side: the generated `.gs` is pushed to a dedicated TEST
   spreadsheet via the Apps Script API (service account, test project
   only) and executed; results are read back through the sheets adapter.
   Gate this behind a marker (`@pytest.mark.live_gs`) so local runs stay
   offline. It MUST pass before any release of the emitter.
4. The reference workbook config is a mandatory parity fixture: the
   generated script must reproduce what `engine/reference/legacy.gs`
   produced (the existing golden files). That closes the loop on where this
   project started.
5. Optional `backup_tab` guard (config flag, default off): before a
   destructive rule, write affected rows to a hidden `_backup` tab in the
   same spreadsheet, keeping the last N runs. This is the self-serve
   tier's only undo. It is NOT a snapshot, and UI copy must not describe
   it as equivalent.
6. Any parity mismatch is fixed in the emitter, never by changing the
   Python evaluator. The evaluator is the reference.

## E. Delivery (UI, extends PATCH-003)

1. On an approved config, Sheet detail and Approvals offer **"Download
   Apps Script"**: returns the `.gs` plus a 4-step install card
   (Extensions → Apps Script → paste → run `installTrigger`).
2. The download view states plainly what the script tier does NOT do (the
   list from B) and shows the generator + config version, so a stale
   script can be identified later.
3. Regeneration: if the config changes, the UI marks earlier generated
   scripts stale and offers a fresh download. We cannot update installed
   scripts. Say so in one sentence; never imply otherwise.
4. A sheet must not run both tiers at once. If a sheet is ACTIVE on the
   server tier, the download view warns that installing the script too
   would make two automations fight over the same tab, and offers to pause
   the server tier. Record which tier a sheet uses (`sheets.tier`).
5. Script-only flow (no sheet access needed): a user may paste a sheet's
   HEADER ROWS ONLY, or upload an xlsx, to compile a config and download a
   script, without sharing a sheet or connecting an account. Header text
   is structure, not cell values; B.7 masking still applies to any sampled
   rows from an uploaded xlsx. Build this AFTER E.1–E.4.
6. (Do not build) Direct push of the generated script via the Apps Script
   API needs the restricted `script.projects` scope. Record as a future
   option; the download path must always remain.

## F. Out of scope

Remote updates to installed scripts · telemetry or callbacks from
generated scripts · cross-file rules in the apps_script target · Excel/VBA
emission · Marketplace add-on · any change to the server target's behavior.

## G. Milestones

- **S7a** Capability matrix + emitter skeleton + sort/format templates;
  unit tests on emitted text; refusal list working.
- **S7b** Consolidate/validate/dedupe/clear templates + installTrigger,
  menu, pre-flight, optional backup tab.
- **S7c** Live parity harness (D.3) green on the reference workbook config.
- **S7d** UI delivery (E.1–E.4).
- **S7e** Script-only enrollment flow (E.5).

Exit for S7: someone who has never shared a sheet or connected an account
can describe an automation, download a script, paste it, and get the same
result the managed tier would have produced, proven by D.3, not by
inspection.

## H. Product note (not a build instruction)

This creates two tiers from one codebase: self-serve (generate + paste,
zero infrastructure per user) and managed (server runtime, drift repair,
undo, audit, fleet view, cross-file rules). Every gauntlet finding that
extends the config schema improves both tiers. The gauntlet stays the
priority as soon as real sheets arrive; S7 is what to build while waiting.
