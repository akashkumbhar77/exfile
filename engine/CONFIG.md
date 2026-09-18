# Engine.gs — config reference and install guide

`Engine.gs` is the only in-sheet code. It is identical in every sheet. Everything
specific to a sheet lives in the config JSON described here. The authoritative
schema is `docs/config.schema.json`, and `backend/app/services/validator.py` is the
authoritative validator. The engine only rejects configs whose basic shape is wrong.

## Where things live

| What | Where | Notes |
|---|---|---|
| Config (source of truth) | Script Properties `engine.config.0..n` | Split into chunks (each property holds at most 9 KB) with a SHA-256 checksum in `engine.config.sha`. A checksum mismatch means the engine refuses to run and logs an error. |
| Config mirror | hidden `_config` tab | For people to read only. Editing it has no effect. |
| Status | `engine.status` | `ACTIVE`, `PAUSED_DRIFT` or `PAUSED_MANUAL`. |
| Debounce flags | `engine.dirty.<rule_id>` | The time of the last edit that affected a debounced rule. |
| Log queue | `engine.queue.*` | At most 200 records. Older records are dropped and counted in `engine.queue.dropped`. |
| Log endpoint | `engine.webhook_url`, `engine.webhook_secret` | Set through `engineSetConfig(json, {webhook_url, webhook_secret})` or the menu. |
| Before-snapshots | hidden `_engine_snapshots` tab | gzip + base64. The last 25 runs are kept. |

## Triggers (installable only)

| Handler | Trigger | Does |
|---|---|---|
| `engineOnEdit` | on edit | Runs the pre-flight, then the `on_edit` rules for the edited tab (only when a listed column was edited) and their `after:` chains. It also marks debounced rules as dirty. |
| `engineTick` | every 1 min | Replays edits that were skipped because the lock was busy, runs debounced rules once their quiet period has passed, and flushes the log queue. |
| `engineOnOpen` | on open | Adds the **⚙️ Automation** menu. Nothing else. |

There are no simple triggers. `schedule` (cron) rules are not run by the engine; the backend worker runs them in M4. They do run from **Run all rules now**.

## Menu

- **Run all rules now**: runs every rule and sets SUMMARY column widths.
- **Rebuild consolidated tabs**: runs the consolidate rules only.
- **Undo last run**: restores every tab captured in the last run's snapshot.
- **Pause automation / Resume automation**: pausing never protects anything. Resume re-checks the headers first.
- **Engine status**: shows the status, config version and number of queued logs.
- **Show header hashes…**: gives the `schema_hashes` values for a hand-written config.
- **Install / replace config…**: paste the approved JSON. Installing a config clears a drift pause.
- **Set log endpoint…**: backend URL and per-sheet secret.
- **Flush logs now**

## Manual install (M2)

1. Make a copy of the workbook.
2. Open **Extensions → Apps Script**. Delete the old code (legacy.gs) and paste `engine/Engine.gs`. Save.
3. Run `engineInstall` once and authorize it. This removes legacy triggers that point at deleted functions.
4. Reload the spreadsheet. Use **⚙️ Automation → Show header hashes…** (header row `2`) to get the hashes. Put the hashes for the governed tabs into the config's `schema_hashes`.
   - The reference config at `backend/tests/fixtures/reference_config.json` contains hashes for the *test fixture*. Replace them with the hashes of your real tabs.
5. Validate the config with the backend validator:
   ```
   cd backend && uv run python -c "import sys,json; from app.services.validator import validate_config; print(validate_config(open(sys.argv[1]).read()).model_dump_json(indent=2))" path/to/config.json
   ```
6. Use **⚙️ Automation → Install / replace config…** and paste the JSON.
7. Run **⚙️ Automation → Run all rules now**.

## M2 acceptance on the real reference workbook

1. Make copy **A**. Keep legacy.gs in it and run `installTrigger`.
2. Make copy **B**. Install Engine.gs as above, then **Run all rules now**.
3. Paste `engine/tools/CompareWorkbooks.gs` into a standalone script project. Set both IDs, then run `compareWorkbooks`. The log must say `PARITY OK`.
4. Optional: make the same edits in A and B, wait 2 minutes, then compare again. The engine rebuilds SUMMARY after the debounce period; legacy rebuilds it on every edit.

## Config essentials (see `docs/SPEC.md` §1 and `docs/DECISIONS.md`)

- **`header_row`, `data_start_row`**: rows are 1-based.
- **`schema_hashes`**: the header hash of every governed tab.
  - A tab without a hash is never touched.
  - A hash mismatch sets `PAUSED_DRIFT`, and nothing runs.
- **`canonical_headers`**: map header variants to one name, using `contains:` / `equals:`. Matching is case-insensitive and trimmed.
- **`enums`**: stages are matched in list order. Unmatched values get `unknown_order`, which must be larger than every stage order.
- **Rules**: `sort`, `format`, `consolidate`, `move`, `copy`, `validate`, `dedupe`, `clear`.
  - **Triggers**: `on_edit {columns}`, `after`, `debounced {quiet_seconds}`, `schedule {cron}`.
  - **`on_edit.columns`** must list every column a chained `format` rule depends on. The reference config lists `FREEZE?` for this reason.
- **`guards.max_rows_per_run`**: if the rows changed by sort/move/copy/dedupe/clear add up to more than this, the whole run is refused and nothing is written.
- **`guards.hold_column`** (default `!hold`): rows with this column set are skipped by every rule.
- **`presentation`** (consolidate only):
  - **Styling**: `title`, colours, `banding`, `column_widths` (keys match headers case-insensitively), `default_column_width`.
  - **`date_format`**:
    - `match_source` copies the number format of the first source tab's date column. The date column is the `sort_like` rule's `type: date` key.
    - Any other value is used as an explicit Sheets number format.

## Run record (sent to `POST /webhooks/log`)

The request body is:
```json
{"sheet_id": "...", "org_id": "...", "engine_version": "0.2.0", "config_version": 3,
 "status": "ACTIVE", "dropped": 0, "flush_failures": 0, "last_flush_error": null,
 "secret": "...", "runs": [ ... ]}
```

Each run record contains:
- **Identity:** `run_id`, `rule_id`, `action`, `trigger_type`, `config_version`
- **Result:** `rows_affected`, `duration_ms`, `status` (`OK`, `ERROR` or `PAUSED_DRIFT`), `error`
- **Details:** `snapshot_ref`, `tabs`, `warnings`, `unknown_values` (values that matched no enum stage, grouped by enum), `drift` (drift records only)

Other record types in `runs`: `error`, `status_change`, `config_installed`, `engine_installed` and `undo`.

## Tests

```
node --test engine/test/engine.test.js          # parity with legacy.gs + engine rules
cd backend && uv run pytest                      # includes Engine.gs vs Python evaluator
```
`engine/test/gas_mock.js` is an in-memory model of the Apps Script services, and
`engine/test/harness.js` runs a script against it with a controllable clock.
