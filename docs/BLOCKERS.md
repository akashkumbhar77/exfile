# BLOCKERS.md

## RESOLVED 2026-09-18: SPEC-PATCH-002 depended on the missing SPEC-PATCH-001
PATCH-001 has been added. S1–S3 replace M2–M4, and M1 stands as done. S1 is built (see
DECISIONS.md, "S1").

## S1 exit criteria on the real workbook (2026-09-18)
- [x] **The dry-run prints a per-tab summary and writes nothing.** The run ended with
  "dry-run: nothing written", and a failed live attempt proved atomicity: nothing was
  applied.
- [x] **The live run applied only the expected changes.** Run `run_20260918T055618_7b7bb040`
  made 2 requests and recoloured 340 cells (the overdue tint on MACHINES rows 32–41 and
  the matching SUMMARY rows).
- [x] **The second run was a no-op**, which also confirms that clearing a colour through
  `*ColorStyle` works on real Sheets.
- [ ] **Owner comparison against the legacy xlsx export.** This is still to do. S2 starts
  once it's done.

Operational notes:
- The service account has to be an editor on SUMMARY's whole-sheet protection
  ("Auto-generated consolidated summary — locked"), not just on the file.
- The sheet's time zone is set to Asia/Calcutta (IST).

## S2 exit criteria on the real workbook (2026-09-18, two sheets watched)
Stack: docker compose Postgres 16 and Redis 7, the migration applied, the watcher and RQ
worker running on the host. Both sheets were registered with the owner's approval.
- [x] **A burst of edits gives exactly 1 run, and the other sheet is never touched.**
  Status edits → `run_20260918T072038_159a4506` and later `run_20260918T081942_810a250f`,
  each about 31s after the change was seen. Sheet 2 had no runs at all.
- [x] **Our own writes never re-trigger a run.** Found and fixed a gap in PATCH-002 A.3: see
  DECISIONS, "S2 live findings". After the fix, both of Drive's change records for our write
  were ignored and no no-op run followed.
- [x] **A renamed header gives PAUSED_DRIFT with nothing written.**
  `run_20260918T083053_ce9659fc` recorded the drift event on MACHINES; sheet 2 stayed ACTIVE.
- [x] **`resume` refuses while drifted and recovers once the header is restored.** The
  follow-up runs were NOOPs.
- [x] **`undo` restores the prior values exactly.** `run_20260918T083549_e3b8f639` undid
  `run_20260918T081942_810a250f`; MACHINES and SUMMARY match the snapshot in both values and
  formats. The undo's own write was ignored.

Open for the owner: confirm the author-based self-write filter (a deviation from A.3's
literal mechanism).

## PARTLY RESOLVED: consolidate `presentation`
Title and header colours are now applied by the evaluator. What follows is the remaining question.

Engine.gs used to apply the SUMMARY styling:
- title banner style
- banding
- column widths
- `date_format`

PATCH-001 retired Engine.gs, and the Python consolidate writes only the title text. The
server-built SUMMARY therefore has the right rows, values and formats, but not legacy's
look. Two options:
- (a) add it as S1/S2 work: new GridOps for column widths, banding and number formats
- (b) park it

**Sharpened by the emitter (2026-09-20).** The Apps Script target could apply banding, widths
and date formats easily - Apps Script has direct APIs for all three. It deliberately does not.
The evaluator is the oracle, and it does not model them, so anything the script did here would
be behaviour no parity run can check: exactly the "an emitter never invents" line in PATCH-004
A.3. So the gap is now a schema/oracle question, not a target question: either the evaluator
learns these three (and the emitter follows), or they leave `Presentation`. Until then the
generated SUMMARY has legacy's colours but not its widths or banding.

## Note: Engine.gs is still in the repo
`engine/` and its tests still run (Node) but aren't used at runtime. Deleting them is a
separate decision for the owner.

## RESOLVED (2026-09-18): S3 LLM provider
The owner chose OpenAI (see DECISIONS "S3" and the CLAUDE.md override note). The agent is
built and tested with a scripted LLM.

## S3 exit criteria on the real workbook: PASSED (2026-09-18)
- [x] **A nonsense instruction ends in a human-readable failure and never an ACTIVE config.**
- [x] **Plain English gives the same behaviour as the hand-written config.** Config v20
  (id 21) matches on every tab once the owner title (PATCH-003 decision (c)) is applied. It's
  left PENDING_APPROVAL, to be approved end to end from the Approvals page (PATCH-003 B1 exit).

## S5 / PATCH-003 B1 Approvals: exit status (2026-09-18)
- [x] **The S3 final enrolment of sheet 2 is approvable end to end from the page.** The owner
  approved config v20 (id 21) in the dashboard at 13:10:11 (`POST /configs/21/approve` returned
  200). v20 is ACTIVE with approved_by "Akash"; v1 is SUPERSEDED; the sheet stays ACTIVE on
  config 21; the `config.approved` event is recorded; nothing was written to the sheet.
- [x] **Rejecting leaves a reason visible in config history, live.** A throwaway proposal (an
  unchanged copy of the active rules, source "throwaway B1 reject check") was rejected from the
  page at 13:21:51 UTC: config id 23 (v22) is REJECTED, with rejected_by "Akash" and the owner's
  reason in `decision_reason`, as returned by `GET /sheets/2/configs`. The `config.rejected` event
  (91) is recorded, and the sheet stayed ACTIVE on its approved config.
  - Incident during the check: the owner clicked Approve instead of Reject on the first
    throwaway (config id 22, v21). Its body was identical to v20 apart from `config_version`, so
    the rules the sheet runs did not change. v21 is now ACTIVE and v20 SUPERSEDED; the history
    keeps this (approvals are not reversed). A second throwaway (v22) was rejected as intended.
    Approve and Reject sit side by side on the decision bar; whether Approve needs a
    confirmation step is an open question for B2.
- [x] **Approve -> ACTIVE -> observed no-op, live.** The owner edited a governed cell on sheet 2 and
  reverted it. The watcher saw the change at 18:54:24 IST and dispatched it after the debounce at
  18:54:54. The worker ran `run_20260918T132454_dd160cd4` under config v21: status NOOP, ops=0,
  2.1 s, nothing written.
  - The watcher and worker had been running pre-google_http code; the watcher logged 4
    connection errors at 17:00 and then went silent. Both were restarted on current code before
    this check.
- **B1 exit: passed on evidence.**
- Live finding fixed during the check: see DECISIONS "google_http" (per-thread Google clients,
  503 google_unavailable, Retry on the preview).

## S5 / PATCH-003 B2 Enroll: exit status (2026-09-18)

- [x] **A fresh workbook reached PENDING_APPROVAL from its URL and an English instruction in
  the Enroll page.** The owner enrolled sheet 3 entirely through the page, reviewed the
  generated proposal, and approved version 1. No CLI enrollment was used.
- [x] **A nonsense instruction has a readable failure path.** Four live enrollment attempts
  completed as `failed`, with no config proposed; the job endpoint exposes the designated
  failure text from its DB-only enrollment record. The Enroll-page test renders that response
  as the readable failure view and the backend contract test covers the same endpoint path.
- [x] **The first watcher run applied the approved proposal only to sheet 3.**
  `run_20260918T140007_a7fe8f51` planned six operations and committed them. Sheet 2 had no
  concurrent run.
- [x] **A later human edit/revert produced an observed no-op.**
  `run_20260918T150407_7a8a978e` is `NOOP`, trigger `change`, zero rows affected, config v1.
  This was a user-originated edit, so it proves the watcher path rather than the self-write
  suppression path.
- **B2 exit: passed on evidence.**

## Next capability: universal numeric conditions (2026-09-18)

The requested numeric-condition capability is now eligible to start once this B2 branch is
merged. The prepared design is a file-independent, typed numeric condition/expression language;
it is not a per-workbook helper-column workaround. Implement the schema, validator,
deterministic evaluator, onboarding skill, readback, and tests together on a focused branch.
