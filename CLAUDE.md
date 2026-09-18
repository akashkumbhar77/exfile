# CLAUDE.md — Sheets Automation Platform (MVP)

This file is the project constitution. Read it before every task. If a request
conflicts with this file, STOP and flag the conflict instead of improvising.
Do not change architectural decisions. Do not "improve" the design. Build to spec.

## What this product is

A platform that automates data organization inside Google Sheets at fleet scale
(target: 300+ sheets for one org). An LLM agent COMPILES per-sheet automation
rules into a declarative JSON config ONCE; a deterministic engine EXECUTES that
config forever. The LLM is never in the runtime hot path.

Mental model: **compiler, not interpreter.** All intelligence is front-loaded to
onboarding and exception handling. Runtime must be boring, fast, auditable.

## Non-negotiable invariants (violating any of these = wrong build)

1. **No LLM calls in the runtime loop.** LLM runs only at: (a) onboarding
   (compile instruction → config), (b) drift repair (propose config patch),
   (c) weekly audit. Never per-edit, never per-run.
2. **The agent's only write artifact is a config JSON.** The agent never gets a
   raw "write cells" tool. It proposes; validator + human approve; engine executes.
3. **One universal Apps Script engine, identical in every sheet.** Per-sheet
   behavior lives ONLY in config. Never generate bespoke script code per sheet.
4. **Pre-flight before rules.** Engine hashes the header rows and compares to the
   hash stored in config BEFORE executing any rule. Mismatch → pause sheet
   (PAUSED_DRIFT), phone home, run nothing. Never run rules against drifted schema.
5. **Never half-apply.** Every rule execution is atomic from the user's view:
   either the batched operation completes or the sheet is left untouched.
6. **Snapshot before destructive ops.** Any rule that moves/deletes/overwrites
   rows stores a before-snapshot first. Every run is revertible.
7. **Logging never blocks the sheet.** Phone-home is async/fire-and-forget with
   local queue + flush-next-run on failure. A logging outage must be invisible
   to the sheet user.
8. **Automation pause never locks humans out.** Pausing rules must never protect
   ranges or block editing (except the by-design locked SUMMARY-type tabs).
9. **Unknown values are safe by default.** Enum values not covered by config
   sort to the bottom with neutral formatting. Never guess, never drop rows.
10. **`org_id` on every table from day one**, even while single-tenant.
11. **Human approval gates**: new configs and drift patches require explicit
    owner approval (dry-run preview first) in the MVP. No auto-apply.
12. **Rows with the `!hold` marker column set are exempt from all rules.**

Privacy invariants added by docs/SPEC-PATCH-002.md B (the patch calls them B.6–B.9):

13. **(B.6) Raw cell values never persist outside their sheet, except in
    snapshots.** They must not appear in logs, errors or stack traces, the runs or
    events tables, metrics, or LLM prompts. Log addresses, counts, hashes and ids
    only. Enforced by `Redacted` and `tests/test_privacy_redaction.py`.
14. **(B.7) LLM masking boundary.** `app/agent/masking.py` masks samples before
    they leave the process. Profiles hold structure and distributions only.
15. **(B.8) Snapshots are encrypted (Fernet, key id stored) and expire**
    (`snapshot_retention_days`, default 30, purged daily).
16. **(B.9) Access scoping.** The system opens only registered file ids, and calls
    no Drive listing or search endpoints beyond the changes feed.

> SPEC-PATCH-001 replaces invariant 3. There is one server-side engine, and rule
> semantics exist in exactly one code path. Engine.gs and Apps Script are retired.
> Patches 001 and 002 win over this file wherever they conflict.

## Locked stack (do not substitute)

- Python 3.12, FastAPI, Pydantic v2
- Postgres (SQLAlchemy 2.x + Alembic migrations)
- Redis + RQ for queue/debounce/scheduling
- google-api-python-client + service account for Sheets/Apps Script APIs
- Anthropic API: claude-haiku-4-5 primary, escalate claude-sonnet-4-6 after
  2 validation failures; prompt caching on sheet profiles
  > **Owner override (2026-09-18):** the agents use the **OpenAI** API instead. The primary
  > and escalation models are settings (`LLM_PRIMARY_MODEL`, `LLM_ESCALATION_MODEL`); the
  > escalation policy is unchanged. Caching works through OpenAI's automatic prompt-prefix
  > caching (profile kept in a stable prefix). See docs/DECISIONS.md "S3".
- Apps Script (V8) for the in-sheet engine — plain JS, no clasp-bundled deps
- Frontend: React + Vite, minimal; server state via TanStack Query
- Snapshots: gzip JSON in Postgres (bytea) for MVP — no S3 yet
- Docker Compose for local dev (api, worker, postgres, redis)

## Repo layout (create exactly this)

```
/backend
  /app
    /api          # FastAPI routers: sheets, configs, runs, webhooks, approvals
    /core         # settings, auth, db session
    /models       # SQLAlchemy models
    /schemas      # Pydantic: ConfigSpec is the crown jewel
    /services     # sheets_client, deployer, dry_run, snapshots, anomaly
    /agent        # onboarding + repair agents, prompts, tools
    /workers      # RQ jobs: execute_run, ingest_log, drift_check, audit
  /alembic
  /tests
/engine           # the universal Apps Script (Engine.gs, Config schema doc)
/frontend         # React dashboard
/docs             # SPEC.md lives here; keep it updated when specs clarify
docker-compose.yml
```

## Definitions (use these words precisely)

- **Profile**: stored structural description of a sheet (tabs, canonical headers,
  column types, enum value distributions, sampled rows metadata).
- **Config**: versioned JSON governing one sheet. Contains schema_hash, rules,
  guards. Validated against ConfigSpec (Pydantic + JSON Schema export).
- **Rule**: one automation unit. Actions v1: sort (with custom stage order),
  move, copy, format (condition → font/bg/strike, row and cell scope),
  consolidate (cross-tab, header canonicalization map, locked target), validate
  (dropdowns), dedupe, clear.
- **Drift classes**: A transient, B structural (hash mismatch), C semantic (new
  enum value / distribution shift), D wrong-but-approved spec (defense =
  snapshots + undo, NOT detection), E engine bug (defense = ringed deploys).

## Engine (Apps Script) rules

- Header detection by canonical keyword matching (see canonicalHeader map in
  config), never by fixed column index.
- All reads/writes as batched range ops (setValues/setFontColors arrays).
  Never per-cell loops.
- Debounced summary rebuild: dirty flag + time trigger, never full rebuild on
  every onEdit.
- Installable triggers only (run as owner). No simple onEdit.
- No silent catch blocks — every caught error is logged to the phone-home queue.
- Config stored in Script Properties; hidden `_config` tab is a mirror for
  human inspection only, never the source of truth.

## Coding conventions

- Type hints everywhere; mypy strict on /backend/app.
- Every service function that mutates a sheet takes and records a run_id.
- Tests: pytest; every rule action gets unit tests against fixture grids;
  dry_run and executor share the same rule-evaluation code path (dry_run =
  evaluate without apply). This sharing is mandatory — no duplicate logic.
- Migrations for every model change. Never auto-create tables at runtime.
- No secrets in code; pydantic-settings from env.
- Commit style: conventional commits. Small, reviewable commits per component.

## Build order (do not reorder; each milestone has exit criteria in docs/SPEC.md)

- M1 Config schema + validator + rule evaluator + dry-run (pure Python, no Google)
- M2 Engine.gs (sort/format/move/consolidate) reading config, manual paste-in
- M3 Backend registry + Sheets client + deployer (push engine+config via
  Apps Script API) + snapshots + undo
- M4 Webhook + debounce + run logs + anomaly checks + drift pause path
- M5 Onboarding agent (profile → compile → validate → dry-run → approval)
- M6 Repair agent (drift diff → patch proposal → approval) + weekly audit job
- M7 React dashboard (fleet view, sheet detail, approvals, activity, undo)

Never start M(n+1) before M(n) exit criteria pass. If blocked, write the
blocker into docs/BLOCKERS.md and stop rather than guessing.

## Explicitly out of scope for MVP (do not build)

Marketplace add-on/sidebar, OAuth end-user flows (service account only),
billing, S3, auto-applied drift patches, multi-region, LangGraph or any agent
framework (plain tool-use loop only), websockets, Kubernetes.
