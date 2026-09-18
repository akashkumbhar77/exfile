---
name: config-basics
description: Top-level config fields, governed tabs (schema_hashes), header canonicalization, tab selectors, guards. Load first, always.
schema_defs: [CanonicalHeader, Guards]
---
# Config basics

## Top-level shape
```json
{"schema_hashes": {"TAB": "auto"}, "header_row": 2, "data_start_row": 3,
 "canonical_headers": [...], "enums": {...}, "rules": [...], "guards": {...}}
```
- The server fills `sheet_id`, `org_id`, `config_version` and the real `schema_hashes` values.
  Write `"auto"` for each governed tab.
- `header_row` / `data_start_row` are 1-based. Take them from the profile's `header_row` (data
  usually starts on the next row). Every governed tab must share the same header row.
- `guards`: keep the defaults `{"max_rows_per_run": 500, "snapshot_destructive": true,
  "hold_column": "!hold"}` unless the instruction says otherwise.

## Governed tabs (`schema_hashes`)
- Only governed tabs are read or written by rules. List every tab the instruction applies to.
- NEVER list a consolidate `target_tab` (it is generated) or a tab that doesn't exist.
- Each governed tab's header row is hashed. If someone later renames a header, the sheet pauses
  safely instead of running rules against the wrong columns.

## Canonical headers
- Rules refer to columns by canonical name. `canonical_headers` maps header variants across
  tabs to one name: `{"canonical": "DUE DATE", "match": ["contains:DUE"]}`.
- Patterns are `contains:X` / `equals:X`: case-insensitive and trimmed.
- **Declare every column any rule, enum, key or condition references**, even when its spelling
  is the same on every tab (e.g. `{"canonical": "TICKET STATE", "match": ["equals:TICKET STATE"]}`).
  The validator rejects references to undeclared columns. Columns no rule references don't
  need entries: they keep their own header text, including in consolidated tabs.
- **A pattern must match at most ONE header per tab.** The server rejects patterns that match
  two headers (e.g. `contains:STATE` would also hit `STATE OF SLA` next to `TICKET STATE`). Check every governed
  tab's header list in the profile; use `equals:` or a longer fragment when needed.
- **Reuse existing spellings** as the canonical name (e.g. `VIP?`, `Due date`). Consolidated
  tabs use canonical names as their column headers, so an invented spelling renames columns.

## Tab selectors (`tabs` / `sources`)
- `"all_with:TICKET STATE"` = every governed tab that has that canonical column. Prefer it when
  the instruction means "every tab with that column".
- Or an explicit list of governed tab names: `["BILLING", "HARDWARE"]`.

## Rows that are never touched
- Rows whose `!hold` column is set are exempt from every rule; the engine enforces this.
- Values matching no enum stage sort to the bottom with neutral formatting, and are never
  dropped.
