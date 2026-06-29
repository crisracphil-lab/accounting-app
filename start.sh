#!/bin/sh
# Ensure persistent data directories exist before starting the server.
# /data is the Fly.io volume mount point; create it locally if absent (dev).
# Runs as root so we can always create subdirs regardless of volume ownership.
mkdir -p /data
mkdir -p /data/uploads/request_attachments
mkdir -p /data/uploads/payment_receipts

# Fix ownership so the app user can read/write all data files.
chown -R bookpoint:bookpoint /data

# Print resolved DB path so it is clearly visible in Fly.io logs.
echo "BookPoint: database path = ${ACCOUNTING_DB:-/data/accounting.db}"

exec su -s /bin/sh bookpoint -c "exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --log-level info"
