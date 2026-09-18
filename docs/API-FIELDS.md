# PATCH-003 page fields → API (audit, 2026-09-18)

This walks through every field that SPEC-PATCH-003 section B puts on screen and lists the
endpoint that supplies it. **Gap** marks fields that the data model couldn't supply before
this change, with how each gap was closed in the same commit. All endpoints live under
`/api/v1` and need `Authorization: Bearer <API_TOKEN>`, except `/health`.

## B1 Approvals (`/approvals`)
| Field | Source |
|---|---|
| Pending list | `GET /configs?status=PENDING_APPROVAL` → `items[]` |
| Sheet title in the list | `items[].sheet.title`. **Gap:** `sheets.title` was never filled. Now set from the spreadsheet's `properties.title` (metadata) at access check and onboarding |
| Who and what proposed it | `items[].source` (`kind`, `model`, `session_id`, `prompt_version`). **Gap:** closed by migration 0003 |
| Plain-English rule list | `GET /configs/{id}/describe` → `rules[].headline/when/details`, `stages`, `guards`, `lines` |
| Dry-run summary per tab | `GET /configs/{id}/dry-run/preview` → `tabs[]`: `rows_to_reorder`, `rows_to_recolor`, `rows_added`/`rows_removed` (moved), `cells_to_write`, `cells_to_recolor`; `warnings` |
| Changed-rows sample (≤20 per tab, before/after, changed cells) | the same endpoint → `tabs[].sample[]`: `row`, `before[]`, `after[]` (`v`, `font`, `bg`, `strike`), `changed[]`, plus `headers`. **Gap:** only counts existed. Now computed on demand from the plan's projected workbook; not persisted, not logged |
| Summary-tab title input (decision (c)) | shown when `has_consolidate`; the current value is in `GET /configs/{id}` → `body` / `summary_title`. Preview it with `?summary_title=` on describe and preview |
| Approve | `POST /configs/{id}/approve {actor, summary_title?}` |
| Reject with a required reason | `POST /configs/{id}/reject {actor, reason}` (422 if empty) |
| The reason visible in config history | `GET /sheets/{id}/configs` → `decision_reason`, `rejected_by`, `rejected_at`. **Gap:** there was no reason column (0003) |
| Approver identity | **Gap:** there are no users (PATCH-003 C). `actor` is a free-text name the UI keeps in memory with the token, and it's recorded on the config |

## B2 Enroll (`/enroll`)
| Field | Source |
|---|---|
| Service-account email plus share instructions | `GET /meta` → `service_account_email`, `share_instructions[]`. **Gap:** not exposed anywhere before |
| URL or ID input | the server extracts the ID from either (`sheet_ref.py`); a tab `gid` is rejected with a hint |
| "Check access" button | `POST /sheets/access-check {sheet}` → `access`: `ok` / `forbidden` / `not_found`, plus `title` and `tabs` |
| Submit | `POST /sheets {sheet, instruction}` → 202 `{job_id, sheet_id}` |
| Progress states | `GET /jobs/{id}` → `state`: `queued` / `profiling` / `compiling` / `validating` / `dry_running` / `proposed` / `failed`. **Gap:** onboarding had no progress hooks; it now publishes to the RQ job's meta |
| Redirect to the approval | `GET /jobs/{id}` → `config_id` once `state` is `proposed` |
| Readable failure | `GET /jobs/{id}` → `failure`, `failures[]` |

## B3 Sheet detail (`/sheets/{id}`)
| Field | Source |
|---|---|
| Title, status chip | `GET /sheets/{id}` → `title`, `status` |
| Pause / Resume | `POST /sheets/{id}/pause`, `POST /sheets/{id}/resume`. While headers still differ, resume returns 409 `still_drifted` |
| "Undo last run" plus confirm (snapshot age, what's restored) | `GET /sheets/{id}` → `undo_last`; `GET /sheets/{id}/undo-preview[?run_id]` → `snapshot_age_seconds`, `tabs`, `expires_at`; `POST /sheets/{id}/undo {run_id?, force?}` |
| Active config in plain English, version, who approved and when | `GET /sheets/{id}` → `active_config` (`version`, `approved_by`, `approved_at`, `describe`) |
| Run history: time, trigger, rule ids, rows affected, status | `GET /sheets/{id}/runs` → `items[]`, one per run (runs rows are per rule). **Gap:** grouping by run |
| Per-run undo, or disabled with the retention tooltip | `items[].undo` → `available`, `reason` (e.g. "snapshots are kept for 30 days; this one has expired"), `expires_at`. **Gap:** derived from snapshot rows, purge tombstones and org retention |
| Open flags | `GET /sheets/{id}` → `flags[]` (`kind`: `drift` / `onboarding_failed`). **Gap:** the flags table is schema-only in the pilot (PATCH-001 B), so flags are derived from sheet state and events |
| Drift: what changed | `GET /sheets/{id}/drift` → `tabs[].changes[]` (`column`, `expected`, `current`). **Gap:** only hashes were stored. Configs now keep a header-text snapshot (`governed_headers`, 0003), compared with a live header read |
| Config history (versions, status, reasons) | `GET /sheets/{id}/configs` |

## B4 Fleet (`/`)
| Field | Source |
|---|---|
| Table: title, status, last run time and result, open flags count, link | `GET /sheets` → `items[]` (`last_run`, `open_flags`, `pending_approvals`, `id`) |
| Counts strip | `GET /fleet/summary` → `active`, `paused`, `paused_drift`, `pending`, `pending_approvals`, `open_flags`, `runs_24h`, `error_rate_24h` |

## Not built (parked or out of scope)
- **Drift-patch approvals:** the repair agent is parked (PATCH-001 B).
- **Webhooks:** replaced by the changes feed (PATCH-002 A).
- **`/flags` write endpoints:** parked.
