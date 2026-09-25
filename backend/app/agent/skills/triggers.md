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
- **When the instruction does not say when rules should run**, give each rule that does not
  follow another `{"debounced": {"quiet_seconds": 60}}` (it runs a minute after edits stop), and
  chain the rest with `after`. The readback states the timing, so the owner can change it.
- The output is an Apps Script the owner installs. It runs rules on edits by people (not on
  changes made by other scripts or imports), a minute or so after a debounce's quiet period, and
  on schedules checked once an hour: a cron minute field must be ONE number, and the rule runs
  once within that hour. Anything more frequent than hourly cannot be done; if the instruction
  explicitly asks for it, reply `CANNOT:` with a plain explanation instead of proposing.
