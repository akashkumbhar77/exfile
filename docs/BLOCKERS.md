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
- [ ] **Rejecting leaves a reason visible in config history, live.** It's covered by the API
  and component tests, but not yet done live because no pending config remains. To do: reject a
  throwaway proposal from the page.
- Live finding fixed during the check: see DECISIONS "google_http" (per-thread Google clients,
  503 google_unavailable, Retry on the preview).
