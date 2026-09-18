# Backend

Server-side engine (SPEC-PATCH-001): the M1 evaluator plans runs, and `app/adapters/sheets_adapter.py`
reads and writes Google Sheets.

## Operator setup (a human does this; the code never does)

1. Create a GCP project.
2. Enable the **Google Sheets API** and the **Google Drive API**.
3. Create a **service account**. You don't need to give it any IAM roles.
4. Create a JSON key for it. Store the key outside the repo, and set
   `GOOGLE_APPLICATION_CREDENTIALS` in `backend/.env` to its absolute path.
5. Make a Google Sheets copy of the reference workbook. Share it with the
   service-account email as **Editor**, and put its id in `.env` as
   `TEST_SHEET_ID`.
6. Generate a snapshot key and set `SNAPSHOT_KEY` (the command is in `.env.example`).

The system opens only spreadsheet ids listed in `TEST_SHEET_ID` / `ENROLLED_SHEET_IDS`
(invariant B.9).

## S1: organize on command

```
uv run python cli.py run --sheet-id <TEST_SHEET_ID> --config <config.json> --dry-run
uv run python cli.py run --sheet-id <TEST_SHEET_ID> --config <config.json>
uv run python cli.py run --sheet-id <TEST_SHEET_ID> --config <config.json>   # expect "0 ops"
```

The config's `sheet_id` must equal `--sheet-id`. Its `schema_hashes` must match the live
headers. If they don't, the run reports `PAUSED_DRIFT` with the expected and actual hash
for each tab, and writes nothing.

Every live run does the following, in order:
1. Saves encrypted before-snapshots under `SNAPSHOT_DIR`.
2. Re-reads the sheet. If the sheet changed since planning, it aborts and writes nothing.
3. Applies every change in a single atomic `batchUpdate`.

### S1 exit checks on the real sheet (manual)

- **Dry-run writes nothing.** In the sheet, open *File → Version history*. No new version
  should appear after `--dry-run`.
- **The live run matches legacy.** Compare the sort order and formatting against the legacy
  xlsx export.
- **The second run is a no-op.** It prints `no changes: 0 ops`.

## Tests

```
uv run python -m pytest -q
uv run mypy --no-sqlite-cache
```

On this machine, Windows Application Control blocks `pytest.exe` and the sqlite DLL, so use
the commands above.
