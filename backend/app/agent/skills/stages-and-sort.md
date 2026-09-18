---
name: stages-and-sort
description: Custom stage orders for status-like columns (enums) and multi-key sort rules. Load when the instruction sorts, orders by status/priority/stage, or colours by stage.
schema_defs: [EnumSpec, EnumStage, SortRule, SortKey]
---
# Stages (enums) and sorting

## Enums: a custom order for a status-like column
```json
"enums": {"TICKET STATE": {"stages": [
  {"value": "OPEN",     "match": "contains:OPEN",   "order": 1},
  {"value": "WAITING",  "match": "contains:WAIT",   "order": 2},
  {"value": "RESOLVED", "match": "contains:RESOLV", "order": 3},
  {"value": "REOPENED", "match": "contains:REOPEN", "order": 4},
  {"value": "CLOSED",   "match": "contains:CLOS",   "order": 5}],
  "unknown_order": 99}}
```
- The enum's key is the canonical column it classifies (here `TICKET STATE`).
- `value` is your clean stage name. Conditions refer to it (`{"enum": "TICKET STATE", "is": "RESOLVED"}`).
- **Match on distinctive fragments**, not full labels, so spelling variants (`Waiting`,
  `2. WAITING ON CUSTOMER`, `waiting-reply`) all match. Use the real labels in the profile's
  `enum` distributions to pick the fragments, and check that each label hits the right stage.
- **Stages are matched in list order, and the first match wins.** If one stage's fragment
  also appears in another stage's labels, list the more specific stage first, or use a longer
  fragment (here `OPEN` would also match `REOPENED`, so a label like `Reopened` needs the REOPENED stage listed
  before OPEN, or OPEN matched with `equals:`).
- `unknown_order` must be greater than every stage `order`. Unknown values sort there with
  neutral formatting.

## Sort rule
```json
{"id": "sort_tickets", "action": "sort", "tabs": "all_with:TICKET STATE",
 "trigger": {"on_edit": {"columns": ["TICKET STATE", "DUE DATE"]}},
 "keys": [{"column": "TICKET STATE", "using_enum": "TICKET STATE"},
          {"column": "DUE DATE", "order": "asc", "blanks": "last", "type": "date"}]}
```
- Keys apply in order; the sort is stable.
- A key with `using_enum` sorts by stage order. Don't combine it with `type`.
- `type`: `date` for date columns (real dates and ISO text), `number`, `text`, or `auto`.
- `blanks`: `first` / `last`. Blanks go there regardless of `asc` / `desc`.
- Values move and formats stay in place, so pair a sort with a format rule chained
  `{"after": "<sort id>"}` whenever rows should be recoloured after moving.
