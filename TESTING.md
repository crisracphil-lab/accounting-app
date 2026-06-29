# BookPoint — Testing Guide

## Running the Test Suite

```bash
python -m pytest tests/ -v
```

Run from the project root (`CRIS_v19/`). The virtual environment must be active:

```bash
.venv\Scripts\activate
python -m pytest tests/ -v
```

## Health Check

Once the server is running, verify it is live:

```
GET http://localhost:8000/api/health
```

Expected response: `{"status": "ok"}`

## Dashboard Stats

```
GET http://localhost:8000/api/dashboard/stats
```

Returns summary counts for runs, transactions, and pending items.

## What the Tests Cover

- **test_bank_reconciliation_general** — bank statement upload and matching logic
- **test_classifier_and_je** — transaction classification and journal entry generation
- **test_closing_fs_sl_analysis** — financial statement closing run analysis
- **test_detailed_reconciliation** — multi-file detailed reconciliation
- **test_invoice_matching** — invoice-to-payment matching
- **test_payment_je_generator** — payment journal entry generation
- **test_payment_requests_portal** — payment request portal endpoints
- **test_ra260_payments** — RA 260 payment processing
- **test_reconciliation** — core reconciliation logic
- **test_reconciliation_workspace_generic** — reconciliation workspace
- **test_runtime_requirements** — policy checks (no mocks, required endpoints, docs)

## Notes

- Tests use a temporary in-memory SQLite database — no production data is touched.
- Skipped tests (`s`) indicate optional features that require additional data files.
- All tests must pass before the server will launch via `setup_and_run.bat`.
