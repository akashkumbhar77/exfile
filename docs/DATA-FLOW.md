# Data flow

*For your IT or security reviewer: what the platform reads, what it keeps, what reaches an
AI model, and how data is deleted.*

## In one paragraph

The platform keeps your Google Sheets organized: it sorts rows, colors them, and builds a
summary tab. It works through one Google **service account** that you share individual
sheets with; it cannot see anything else in your Drive. It reads a sheet, works out the
changes, and writes them straight back. Your cell contents stay in your sheet. The only
exception is short-lived, **encrypted** "undo" copies, which are deleted automatically
after 30 days (configurable).

## What is read

| What | From | Why |
|---|---|---|
| Cell values, font/fill colors, strikethrough, dropdowns, and protection of the sheets you shared | Google Sheets API, one request per run | To decide how to organize the sheet |
| The list of files that changed recently, and when | Google Drive changes feed (file IDs and times only) | To notice edits; one request every 30s covers every sheet |
| A shared sheet's "last modified" time | Google Drive file metadata | To recognize its own edits so it never triggers itself |

**Access limits:**
- **Only shared sheets.** The service account can open only the sheets you share with it.
  It opens only sheets registered in the platform, and refuses anything else.
- **No searching or listing.** It never searches or lists your Drive's files.
- **Read-only Drive access.** Its Drive permission is read-only file metadata
  (`drive.metadata.readonly`), so it can't read file contents through Drive. Sheet contents
  are read only through the Sheets API, and only for registered sheets.

## What is stored

| Stored item | Holds cell contents? | Kept for |
|---|---|---|
| Sheet registry: sheet ID, title, status (active or paused) | No | While the sheet is enrolled |
| Automation settings ("config"): column names, status labels, sort order, colors, and who approved them | No; column headers and status labels only | Every approved version is kept for audit |
| Run history: time, rule, rows affected, outcome, short error codes | No | Indefinitely |
| Event log: changes noticed, pauses, approvals, undos | No; IDs, counts and hashes only | Indefinitely |
| Change fingerprints: one hash per tab | No; a one-way hash from which contents can't be recovered | Replaced on every run |
| **Undo snapshots**: a tab's state just before a change | **Yes**, encrypted (Fernet: AES-128 plus HMAC). Each snapshot records which key sealed it. | **30 days** by default, per organization, then deleted |

**Application logs** record sheet IDs, tab names, cell addresses, counts, hashes and
outcomes. They never record cell contents, and every build runs an automated test that
enforces this.

## What reaches an AI model

- **Day to day:** nothing. Sheets are organized by fixed rules, and no AI model runs when a
  sheet changes.
- **When a new sheet is enrolled from a plain-English instruction:** the model receives the
  sheet's structure (tab names, column headers, status labels and counts) and a few sample
  rows. The samples are **masked on our server before they are sent**:
  - Names and companies become placeholders such as `Company_A` and `Person_B`.
  - Emails and phone numbers become fake values of the same shape.
  - Numbers become random values of similar size.
  - Dates are shifted by a random offset, which keeps their order.
- **Stored sheet descriptions** contain structure and value distributions only, never
  sample rows.

## How data is deleted

- **Undo snapshots:** a daily job deletes each snapshot's contents after the retention
  period. Undoing an expired run then fails with a clear message instead of restoring
  anything.
- **Offboarding a sheet:** stop sharing the sheet with the service account, and access ends
  immediately. The registry, settings and run history hold no cell contents, and we can
  delete them on request.
- **Encryption key:** the snapshot key is held outside the database. Deleting the key makes
  every remaining snapshot unreadable at once.

## Safety behaviors you can rely on

- **All or nothing:** every change a run makes is one all-or-nothing update. A failed run
  leaves the sheet untouched.
- **Header check:** if someone renames or removes a column the automation depends on, the
  sheet is paused before anything runs, and it stays paused until an owner resumes it.
- **Snapshot first:** a snapshot is saved before any change is written, so every run can be
  undone.
- **Editing is never blocked:** pausing never locks people out of a sheet. The only locked
  tab is the generated summary tab, which is rebuilt automatically.
- **Human approval:** a person must approve a new sheet's settings after previewing the
  result (a "dry run").
