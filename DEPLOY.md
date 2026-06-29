# BookPoint — Step-by-Step Deployment Guide

This guide walks you through deploying BookPoint to **Fly.io** from a Windows PC,
including first-time setup, ongoing deployments, and day-to-day operations.

---

## Prerequisites

Install these tools once on your Windows machine.

### 1. Git

Download from https://git-scm.com/download/win and install with default options.
After installation, open **Git Bash** and verify:

```
git --version
```

### 2. Fly.io CLI (`flyctl`)

Open **PowerShell** as administrator and run:

```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

Verify:

```
flyctl version
```

### 3. Create a free Fly.io account

Go to https://fly.io and sign up (no credit card required for the free tier).

Log in from PowerShell:

```
flyctl auth login
```

This opens a browser — click **Approve** to authenticate.

---

## First-Time Setup (run once)

Do this only the very first time you deploy.

### Step 1 — Create the Fly.io app

```
flyctl apps create bookpoint --org personal
```

If `bookpoint` is already taken, choose another name and update the `app =` line
in `fly.toml` to match.

### Step 2 — Create the persistent volume

This volume stores your SQLite database and all uploaded files. It survives
redeploys and restarts.

```
flyctl volumes create bookpoint_data --size 3 --region sin --app bookpoint
```

`sin` = Singapore (closest region to the Philippines). The volume is 3 GB — enough
for years of data.

### Step 3 — Set the secret key

BookPoint signs session cookies with a random secret. Generate one and store it
as a Fly.io secret (never put it in code or fly.toml):

```
flyctl secrets set ACCOUNTING_SECRET_KEY="$(openssl rand -hex 32)" --app bookpoint
```

On Windows without OpenSSL, you can generate a key in Python:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output, then:

```
flyctl secrets set ACCOUNTING_SECRET_KEY="paste-your-key-here" --app bookpoint
```

### Step 4 — Set optional email secrets

If you want email notifications (payment receipts, etc.) to work, set your SMTP
credentials:

```
flyctl secrets set ACCOUNTING_SMTP_HOST="smtp.gmail.com" --app bookpoint
flyctl secrets set ACCOUNTING_SMTP_PORT="587" --app bookpoint
flyctl secrets set ACCOUNTING_SMTP_USER="your-email@gmail.com" --app bookpoint
flyctl secrets set ACCOUNTING_SMTP_PASS="your-app-password" --app bookpoint
flyctl secrets set ACCOUNTING_FROM_EMAIL="your-email@gmail.com" --app bookpoint
```

For Gmail, generate an **App Password** at https://myaccount.google.com/apppasswords
(requires 2FA to be enabled on your Google account).

### Step 5 — Initialize a Git repository

Open Git Bash in `C:\path\to\BookPoint` and run:

```bash
git init
git add .
git commit -m "Initial commit"
```

### Step 6 — Deploy for the first time

```
flyctl deploy --app bookpoint
```

Fly.io builds the Docker image in the cloud and launches your app. This takes
2–4 minutes on the first deploy.

### Step 7 — Create the first admin account

After the first deploy, open the app in a browser:

```
flyctl open --app bookpoint
```

You will see a login page with a **"Create first admin account"** button
(visible only when no users exist). Create your admin account there.

---

## Ongoing Deployments (after every code change)

Whenever you update the code:

```bash
git add .
git commit -m "Brief description of what changed"
flyctl deploy --app bookpoint
```

Fly.io performs a **rolling deploy** — the old version keeps serving traffic
until the new version passes its health check, so there is no downtime.

---

## Automatic Deployments via GitHub Actions (optional)

If you push the code to GitHub, every push to `main` automatically deploys.

### Step 1 — Create a GitHub repository

Go to https://github.com/new, create a private repository (e.g. `bookpoint`).

### Step 2 — Add the remote and push

```bash
git remote add origin https://github.com/YOUR_USERNAME/bookpoint.git
git branch -M main
git push -u origin main
```

### Step 3 — Add the Fly.io token to GitHub

Generate a deploy token:

```
flyctl tokens create deploy --app bookpoint
```

Copy the token output.

In your GitHub repository, go to **Settings → Secrets and variables → Actions**,
click **New repository secret**, name it `FLY_API_TOKEN`, and paste the token.

From this point on, every `git push` to the `main` branch automatically deploys.

---

## Checking the Live App

### Open in browser

```
flyctl open --app bookpoint
```

### Check app status

```
flyctl status --app bookpoint
```

### View live logs

```
flyctl logs --app bookpoint
```

Add `-f` to follow logs in real time:

```
flyctl logs -f --app bookpoint
```

### SSH into the running machine (for debugging)

```
flyctl ssh console --app bookpoint
```

---

## Database Operations

The SQLite database lives at `/data/accounting.db` on the Fly.io volume
(`bookpoint_data`).

> ⚠️ **Data safety warning**
>
> The `bookpoint_data` volume is the **only** copy of your accounting data.
> Deleting the volume with `flyctl volumes destroy` permanently erases
> every transaction, journal entry, user account, and uploaded file.
> **Always take a backup before any destructive operation.**

### Backup — binary copy (fastest)

Downloads the whole database file to your local machine:

```
flyctl ssh sftp get /data/accounting.db ./accounting_backup.db --app bookpoint
```

### Backup — SQL dump (most portable)

Produces a plain-text `.sql` file that can be loaded into any SQLite installation:

```
flyctl ssh console --app bookpoint -C \
  "sqlite3 /data/accounting.db .dump" > accounting_backup.sql
```

To restore from a SQL dump:

```
flyctl machine stop --app bookpoint
flyctl ssh console --app bookpoint -C \
  "sqlite3 /data/accounting.db < /dev/stdin" < accounting_backup.sql
flyctl machine start --app bookpoint
```

### Upload a database (e.g. to restore from backup)

Stop the machine first to avoid corruption:

```
flyctl machine stop --app bookpoint
flyctl ssh sftp put ./accounting_backup.db /data/accounting.db --app bookpoint
flyctl machine start --app bookpoint
```

### Run a SQLite query directly

```
flyctl ssh console --app bookpoint -C "sqlite3 /data/accounting.db 'SELECT COUNT(*) FROM bank_transactions'"
```

---

## Scaling and Cost

BookPoint is configured for **zero-cost idle**:

- `auto_stop_machines = "stop"` — the VM shuts down after a few minutes of no traffic.
- `auto_start_machines = true` — it starts again in ~1 second when a request arrives.
- `min_machines_running = 0` — allows full idle-stop.

With this setup, a lightly used internal tool costs **$0/month** within Fly.io's
free allowance (3 shared VMs free).

If you need the app to stay always-on (no cold-start delay), set:

```
flyctl scale count 1 --app bookpoint
```

This costs roughly **$2–3/month** for a `shared-cpu-1x` with 256 MB RAM.

---

## Secrets Reference

| Secret name | Purpose | Required |
|---|---|---|
| `ACCOUNTING_SECRET_KEY` | Signs session cookies (32-char hex) | **Yes** |
| `ACCOUNTING_SMTP_HOST` | SMTP server for email notifications | No |
| `ACCOUNTING_SMTP_PORT` | SMTP port (usually 587) | No |
| `ACCOUNTING_SMTP_USER` | SMTP username / email address | No |
| `ACCOUNTING_SMTP_PASS` | SMTP password or app password | No |
| `ACCOUNTING_FROM_EMAIL` | Sender address for outgoing emails | No |

View all secrets (names only, not values):

```
flyctl secrets list --app bookpoint
```

Update a secret at any time:

```
flyctl secrets set ACCOUNTING_SECRET_KEY="new-value" --app bookpoint
```

Changing a secret restarts the app automatically.

---

## Environment Variables Reference

These are set in `fly.toml` (safe to commit — no credentials):

| Variable | Value | Purpose |
|---|---|---|
| `ACCOUNTING_DB` | `/data/accounting.db` | Path to the SQLite database |
| `ACCOUNTING_UPLOAD_DIR` | `/data/uploads` | Path for uploaded bank files |

---

## Troubleshooting

### App won't start

Check the logs:

```
flyctl logs --app bookpoint
```

Common causes:
- `ACCOUNTING_SECRET_KEY` secret not set → set it with `flyctl secrets set ...`
- Volume not created → run `flyctl volumes create ...`
- Python import error → check logs for the exact error, fix the code, redeploy

### Database is empty after redeploy

The volume was not created, or was accidentally deleted. Restore from a backup
(see Database Operations above) or start fresh — create the first admin account
again via the UI.

### Login locked out

SSH into the machine and unlock via SQLite:

```
flyctl ssh console --app bookpoint -C \
  "sqlite3 /data/accounting.db \"UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE username='admin'\""
```

### Out of disk space

Check volume usage:

```
flyctl ssh console --app bookpoint -C "df -h /data"
```

Extend the volume:

```
flyctl volumes extend VOLUME_ID --size 6 --app bookpoint
```

Get the volume ID from `flyctl volumes list --app bookpoint`.

---

## Quick-Reference Cheat Sheet

```bash
# Deploy
git add . && git commit -m "fix: ..." && flyctl deploy --app bookpoint

# Open app
flyctl open --app bookpoint

# Live logs
flyctl logs -f --app bookpoint

# SSH
flyctl ssh console --app bookpoint

# Backup DB
flyctl ssh sftp get /data/accounting.db ./backup.db --app bookpoint

# Set a secret
flyctl secrets set KEY="value" --app bookpoint

# Scale to always-on
flyctl scale count 1 --app bookpoint
```
