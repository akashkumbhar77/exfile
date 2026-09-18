---
name: consolidate
description: Consolidate many tabs into one generated, locked summary tab (column alignment by canonical header, prepended source column, derived columns, sorting/colouring like the sources, title banner). Load when the instruction combines tabs into a summary.
schema_defs: [ConsolidateRule, PrependColumn, DerivedColumn, Presentation]
---
# Consolidate: one generated summary tab

```json
{"id": "all_tickets", "action": "consolidate", "trigger": {"debounced": {"quiet_seconds": 120}},
 "sources": "all_with:TICKET STATE", "target_tab": "ALL TICKETS",
 "prepend_columns": [{"name": "QUEUE", "value": "tab_name"}],
 "derived": [{"name": "Category", "fill_if_empty": "tab_name"}],
 "sort_like": "sort_tickets", "format_like": "ticket_colours", "lock": true}
```
- **The target is regenerated from scratch** on every rebuild, then locked for humans (`lock`).
  Never edit it with other rules.
- **Targets are never rule inputs.** Don't list `target_tab` in `schema_hashes`, in any rule's
  `tabs`, or in `sources`. To sort or colour the target like the sources, set `sort_like` /
  `format_like` to those rules' ids.
- **Columns.** The header is the union of the sources' canonical column names, in first-seen
  order. Columns are aligned by canonical name, so name variants merge. `prepend_columns` go
  first; `derived` columns are appended if missing. `fill_if_empty: tab_name` puts the source
  tab's name into blank cells.
  - Use the existing target's header spellings (the profile lists the target tab's headers if
    it exists), so the rebuilt tab keeps the same column names.
- Empty rows and held rows are skipped.
- **`presentation`** is optional. `title` is written into row 1 when `header_row > 1`, and the
  title and header colours have sensible defaults. You cannot see an existing title: leave
  `title` unset unless the instruction gives one.
- Use a `debounced` trigger (e.g. 120 s), so the summary rebuilds once edits settle.
