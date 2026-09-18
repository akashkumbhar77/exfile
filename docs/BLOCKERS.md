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

## OPEN: S2 live exit criteria need Postgres and Redis on a machine you control
S2 is built, and the exit criteria pass in tests (backend/tests/test_s2_watch.py: real
Postgres, fake Redis with real RQ jobs, two sheets watched at once). The live criteria on
the real workbook still need a running Postgres and Redis. This machine has neither, and
no Docker or WSL. Options:
- (a) install Docker Desktop and use `docker-compose.yml`, or
- (b) run a native Redis (e.g. Memurai) with the embedded Postgres.

The live criteria also need a **second enrolled sheet**, because PATCH-002 A.5 requires the
tests to run with at least two sheets watched.

Must be verified live (a fake can't prove these):
- Files shared with the service account appear in its Drive changes feed.
- How far `modifiedTime` lags behind our own write.

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
