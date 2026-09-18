---
name: conditions-and-format
description: Condition syntax (enum/date/value/all/any/not) and format rules (row and cell colours, strikethrough, layering). Load when the instruction colours, highlights, strikes, or uses any condition.
schema_defs: [FormatRule, RowFormatRule, CellFormatRule, Style, EnumCondition, DateCondition, ValueCondition, AllCondition, AnyCondition, NotCondition]
---
# Conditions and formatting

## Conditions: flat objects whose values are strings
| kind | shape |
|---|---|
| stage | `{"enum": "TICKET STATE", "is": "RESOLVED"}` (`is` = a stage `value`; `column` defaults to the enum name) |
| date | `{"date": "DUE DATE", "before": "today"}` or `"after"`; the value is `today` or `YYYY-MM-DD` |
| value | `{"column": "VIP?", "equals": "YES"}`, `{"column": "X", "contains": "abc"}`, `{"column": "X", "is_blank": true}` |
| combine | `{"all": [c1, c2]}`, `{"any": [...]}`, `{"not": c}` |

- `date` is the column name as a **string**. WRONG: `{"date": {"column": "DUE DATE", ...}}`.
- Exactly one of `before` / `after`. `before today` = strictly earlier than today;
  `after D` = on or after the day following D. Only real date cells match.
- A value condition has exactly one of `equals` / `contains` / `is_blank`. Comparisons are
  trimmed and case-insensitive.
- Inside `cell_rules`, a value condition without `column` tests that cell itself.

## Format rule
```json
{"id": "ticket_colours", "action": "format", "tabs": "all_with:TICKET STATE",
 "trigger": {"after": "sort_tickets"},
 "row_rules": [
   {"when": {"enum": "TICKET STATE", "is": "OPEN"}, "font": "#1155CC"},
   {"when": {"enum": "TICKET STATE", "is": "CLOSED"}, "font": "#666666", "strike": true},
   {"when": {"all": [{"enum": "TICKET STATE", "is": "OPEN"},
                     {"date": "DUE DATE", "before": "today"}]},
    "font": "#FFFFFF", "background": "#CC0000"}],
 "cell_rules": [{"column": "VIP?", "when": {"equals": "YES"}, "font": "#B45F06"}]}
```
- Style fields: `font` (`#RRGGBB`), `background` (`#RRGGBB`, or `"none"` to clear the fill),
  `strike` (bool). An omitted field leaves that attribute as is.
- **Layering, per attribute (later wins):** `default` → every matching `row_rules` entry in
  list order → every matching `cell_rules` entry. Put general rules FIRST and exceptions
  LAST. In the example, the overdue rule comes after the blue OPEN rule, so overdue open tickets
  end up white on red.
- `default` is already neutral (black text, no fill, no strike) and applies to every data
  row, so rows no rule matches are reset to neutral. You rarely need to set it.
- The title and header rows are never formatted. Held rows keep their formatting.
