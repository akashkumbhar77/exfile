# BLOCKERS.md

## RESOLVED 2026-09-18: SPEC-PATCH-002 depended on the missing SPEC-PATCH-001
PATCH-001 has been added. S1–S3 replace M2–M4, and M1 stands as done. S1 is built (see
DECISIONS.md, "S1").

## OPEN: S1 exit criteria need a human with Google access
There are no GCP credentials in this environment. All three S1 exit criteria pass against
the in-memory Sheets fake (`backend/tests/test_sheets_live.py`), but they haven't been run
on the real converted workbook. Operator steps are in `backend/README.md`. S2 can't start
until these pass on the real sheet.

Things the fake can't prove, so check them on the first real run:
- Clearing a fill or font colour through `backgroundColorStyle` / `foregroundColorStyle` in
  the field mask also clears the legacy `backgroundColor` / `foregroundColor`.
- The second run reports `0 ops`. If it doesn't, the likeliest cause is a colour or number
  round-trip difference.

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
