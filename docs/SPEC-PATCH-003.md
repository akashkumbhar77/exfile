# SPEC-PATCH-003.md — Frontend (S5)

Read after CLAUDE.md, SPEC.md, PATCH-001, PATCH-002. Supersedes SPEC
section 7 (M7). S5 may run in parallel with S4 (generalization gauntlet);
the gauntlet's enrollments MUST be performed through this UI once it
stands — that is its acceptance test.

## A. Principles

1. **Approval screen first.** Build order: Approvals → Enroll → Sheet
   detail → Fleet. Do not start a later page before the earlier one meets
   its exit criteria.
2. **Plain English is rendered, never generated.** The rule readback is
   produced by deterministic template functions per action type
   (`sort` → "Keep <tabs> sorted by <keys>…", `format` → "Color rows
   where…", `consolidate` → "Rebuild <tab> from <sources>…", etc.),
   implemented in the backend (`app/services/describe.py`) and unit
   tested against the reference config. No LLM in the read path.
3. **Cell values on screen, never in storage.** Dry-run previews and
   sample rows are fetched on view and held in client memory only.
   Frontend must not write them to localStorage, analytics, or console;
   backend must not persist preview payloads (B.6 applies). Add a lint
   test greping the frontend for console.log/localStorage on preview
   data paths.
4. **Undo is a first-class UI element**, visible on every run row and on
   the sheet header ("Undo last run"), with a confirm dialog showing the
   snapshot age and what will be restored.
5. Stack per CLAUDE.md: React + Vite + TanStack Query. Styling: one
   lightweight approach (Tailwind or CSS modules — pick one, log it).
   Polling via TanStack Query refetchInterval (5s on active pages).
   No websockets, no state library beyond Query, no component library
   heavier than headless primitives.

## B. Pages and contracts

### B1. Approvals (/approvals)
List of configs with status PENDING_APPROVAL (and later, drift patches).
Detail view per item:
- Plain-English rule list (from describe endpoint)
- Dry-run summary per tab (rows to reorder, cells to recolor, rows to
  move; warnings)
- Changed-rows sample: up to 20 rows per tab, before/after values with
  changed cells highlighted
- Optional "Summary tab title" text input (decision (c): user-supplied,
  stored on the config, never model-read)
- Approve button; Reject button requiring a reason
API gaps to add: GET /configs/{id}/describe (template readback),
GET /configs/{id}/dry-run/preview (summary + sample rows, computed on
demand, not persisted). Approve/reject endpoints exist per SPEC §3.
Exit: the S3 final enroll of sheet 2 is approvable end-to-end from this
page, and rejecting leaves a reason visible in config history.

### B2. Enroll (/enroll)
Form: sheet URL or ID (accept both; extract ID), instruction textarea,
inline copy-able service-account email with share instructions and a
"check access" button (backend attempts a metadata read and reports
ok/forbidden). Submit → agent job → redirect to the approval when the
proposal lands (poll job status; show compile progress states: profiling
/ compiling / validating / dry-running; show the human-readable failure
on a nonsense instruction).
API gaps: POST /sheets returns a job id; GET /jobs/{id} with state +
result config id + failure text. Access-check endpoint.
Exit: a fresh sheet goes from URL + English to PENDING_APPROVAL without
touching the CLI; a nonsense instruction shows its readable failure in
the UI.

### B3. Sheet detail (/sheets/{id})
Header: title, status chip, Pause/Resume, Undo last run.
Sections: active config in plain English (with version + who approved,
when); run history table (time, trigger, rule ids, rows affected,
status, per-run Undo where a snapshot exists and is unexpired — expired
undo disabled with tooltip stating retention); open flags (PAUSED_DRIFT
shows what changed and a Resume-after-fix affordance); config history
(versions with status and reasons).
Exit: the S2 drift scenario (rename header → PAUSED_DRIFT → fix →
resume) is fully operable from this page; an undo performed here
restores the sheet (verified live once).

### B4. Fleet (/)
Table: sheet title, status chip, last run time + result, open flags
count, link to detail. Enroll button. Counts strip (active / paused /
pending approvals). Uses GET /fleet/summary + GET /sheets.
Exit: with >= 5 sheets enrolled (gauntlet), the ops view answers "is
everything healthy?" at a glance.

## C. Auth (pilot-grade)

Static bearer token from env, entered once in the UI, held in memory
(not localStorage), sent on every request. No users, roles, or sessions.
(Future note, do not build: real auth arrives with public OAuth.)

## D. Out of scope for S5

Login/user management · billing · settings pages · templates gallery ·
auto-suggest checkbox onboarding (post-gauntlet, needs species data) ·
charts/analytics · websockets · mobile-specific layouts (desktop-first,
readable on tablet).

## E. Definition of done

All four pages' exit criteria pass; the S4 gauntlet's first three sheets
were enrolled and approved entirely through the UI; pytest + frontend
lint/build green in CI; no cell-value persistence findings from the B.6
lint tests extended to the new endpoints.
