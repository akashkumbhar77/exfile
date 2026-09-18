---
name: move-copy-cleanup
description: Move or copy matching rows to another tab, dedupe by key columns, clear cells, add dropdown validation. Load only when the instruction moves/archives/copies rows, removes duplicates, clears values, or adds dropdowns.
schema_defs: [MoveRule, CopyRule, DedupeRule, ClearRule, ValidateRule]
---
# Move, copy, dedupe, clear, validate

- **move**: `{"action": "move", "tabs": [...], "when": <condition>, "to_tab": "ARCHIVE", "position": "bottom"}`
  - Removes matching rows from the source tabs and appends them to `to_tab`, which must be
    governed.
  - Values are aligned by canonical header. A non-empty value with no matching target column
    aborts the rule, so data is never dropped.
- **copy**: like move, but keeps the source rows. It needs `key_columns`, so that rerunning
  never copies the same row twice.
- **dedupe**: `{"action": "dedupe", "tabs": [...], "key_columns": ["ORDER NO"], "keep": "first"}`.
  Rows whose key cells are all empty are never removed.
- **clear**: `{"action": "clear", "tabs": [...], "when": <condition>, "columns": ["REMARKS"]}`.
  Blanks those cells and never deletes rows.
- **validate**: `{"action": "validate", "tabs": [...], "column": "TICKET STATE", "from_enum": "TICKET STATE"}`
  (or `"values": [...]`), plus `allow_invalid`. Adds dropdowns and never changes values.
- All of these are snapshotted before running, and count towards `guards.max_rows_per_run`.
