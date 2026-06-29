# Accounting Automation - Bank Module

A local web app for accountants. Upload statement/payment register files,
get back a normalized transaction list with supplier matches and balanced
draft journal entries, ready to review and approve.

Runs entirely on your computer at `http://localhost:8000`. No cloud, no server.
SQLite stores everything in a single `accounting.db` file next to the app.

## What it does

1. **Parse** the bank export (handles all 4 sheets, deduplicates by Transaction ID,
   handles flexible statement and payment register layouts).
2. **Match** each transaction to a supplier from your master list (Cella,
   Globe Telecom, PAGCOR, Insula West, BIR, etc.) using exact + substring
   alias matching with confidence scores.
3. **Classify** by configurable rules (PAYROLL → Salaries, BIR EPAYMENT
   → Taxes, ENCASHMENT → Petty Cash, etc.). Unknown ones go to a Suspense
   account so they're visible, not silently miscategorized.
4. **Generate** balanced double-entry draft journal entries linked to the
   source transaction and source file.
5. **Audit** every upload, match, and approval into an immutable log.

Validated against your real March 2026 statement: **219 transactions, 20
supplier-matched, 219 balanced JEs, 84 in suspense for manual review.**

## Setup

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/) — check "Add Python to PATH".
2. Double-click **`setup_and_run.bat`**. The first run creates a virtual
   environment, installs FastAPI/Uvicorn/openpyxl/Jinja2/pytest, runs all
   29 tests, then launches the server.
3. Your browser opens at `http://localhost:8000`.

Subsequent runs skip steps 1–2 and just start the server (~2 seconds).

## How to use it

- **Dashboard** (`/`): counts of uploads, transactions, JEs, and suspense items.
- **Upload** (`/upload`): drop in an .xlsx UB statement. The app refuses to
  re-ingest a file with the same SHA-256.
- **Transactions** (`/transactions`): full list with filters by status, supplier,
  and free-text search. Click any row for detail + the generated JE.
- **Journal Entries** (`/journal-entries`): list draft / approved JEs.
  Approve from the detail page.
- **Suppliers** (`/suppliers`): your supplier master + aliases. To add a new
  one, edit `app/db.py` (`SEED_SUPPLIERS`), delete `accounting.db`, and
  re-run. UI-based supplier management ships in the next iteration.
- **Audit Log** (`/audit-log`): every action with full detail JSON.

## Files

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app + routes |
| `app/db.py` | SQLite schema + seed data (chart of accounts, suppliers, classification rules) |
| `app/parsers/bank_statement_generic.py` | Generic statement/payment parser |
| `app/services/supplier_matcher.py` | Confidence-scored alias matcher |
| `app/services/classifier.py` | Rule-based account classifier |
| `app/services/je_generator.py` | Balanced double-entry JE builder |
| `app/services/file_upload.py` | Ingestion pipeline |
| `app/templates/` | Jinja2 HTML pages |
| `app/static/style.css` | Plain CSS |
| `tests/` | 29 tests against the real UB file |
| `setup_and_run.bat` | Windows one-click installer |
| `requirements.txt` | Python dependencies |

## Customising

Edit `app/db.py`, then delete `accounting.db` and re-run to apply seeds:

- **`SEED_ACCOUNTS`** — add/rename chart of accounts entries.
- **`SEED_SUPPLIERS`** — add suppliers + their default expense accounts + aliases.
- **`SEED_RULES`** — add classification patterns (regex or keyword) → account.

Tax rates aren't applied here. Bank transactions reflect cash movement only;
VAT/EWT splits belong to the supplier-invoice app, not the bank module.

## Constraints honoured

- **No mock data.** Every test uses the real `032026 UB PESO.xlsx` you uploaded.
- **No fallback to dummy values.** Unknown transactions go to Suspense (visible),
  not a default expense account.
- **All errors thrown.** Parser errors, missing required fields, unbalanced
  JEs, and duplicate uploads all raise typed exceptions visible in the UI.
- **All APIs callable in real conditions.** Server runs locally, calls real SQLite,
  ingests real bank files.
- **Testing method provided.** `python -m pytest tests/ -v` — 29 tests, all passing.

## What's deferred to a future session

- PDF bank statement parsing (CSV/XLSX first; PDF formats vary too much without samples)
- Historical journal pattern matching (needs accumulated history)
- AI fallback classification for suspense items (rules-first as agreed)
- Reconciliation engine (next priority module)
- GGR reconciliation
- Approval workflow UI polish (state transitions exist; review queue UI is minimal)
- Supplier CRUD UI (currently seed-file only)


## Runtime data policy

- Mock data is not used in runtime code.
- Dummy/fallback data is not used when parsing or reconciling.
- Parser, upload, duplicate, validation, and database errors are raised explicitly.
- JSON API checks use the live SQLite database (`/api/health`, `/api/dashboard/stats`).
- See `TESTING.md` for automated and real-condition smoke testing.


## First-time admin setup

This package does not include any default login credentials. On first launch, open the app and you will be redirected to `/setup` to create your own admin username and password. After the first active user is created, `/setup` is locked and normal sign-in is used.

If you need to start over in a fresh deployment, stop the app and remove the local `accounting.db` file before launching again. Keep a backup first if it contains real data.
