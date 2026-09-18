---
name: triggers
description: When rules run - on_edit (which columns), after (chains), debounced (quiet period), schedule (cron). Load for every config; every rule needs exactly one trigger.
schema_defs: [OnEditTrigger, OnEditSpec, AfterTrigger, DebouncedTrigger, DebouncedSpec, ScheduleTrigger, ScheduleSpec]
---
# Triggers: every rule has exactly one

| trigger | shape | use for |
|---|---|---|
| on_edit | `{"on_edit": {"columns": ["TICKET STATE", "DUE DATE"]}}` | rules that react to edits of those canonical columns (empty list = any column) |
| after | `{"after": "sort_tickets"}` | run right after another rule, e.g. recolour after a sort |
| debounced | `{"debounced": {"quiet_seconds": 120}}` | expensive rebuilds (consolidate) once edits have stopped |
| schedule | `{"schedule": {"cron": "0 7 * * *"}}` | time-based rules (5-field cron) |

- **Every column a chained rule reads must be in the parent's `on_edit.columns`.** For example,
  if a format rule chained after the sort reads `VIP?`, the sort's `on_edit` must list
  `VIP?`, or editing that column would never recolour.
- `after` chains may not form cycles, and must reference an existing rule id.
- Rule ids are lowercase `snake_case`.
- The platform also re-runs edit-driven rules (on_edit and debounced) whenever the file
  changes, so an organized sheet stays organized. The trigger choice mainly expresses intent
  and ordering.
