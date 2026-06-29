    $flyctl = "$env:USERPROFILE\.fly\bin\flyctl.exe"
$app    = "bookpoint-rac"

Write-Host "=== BookPoint: Reset deployed data on Fly.io ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "This will permanently delete:" -ForegroundColor Yellow
Write-Host "  /data/accounting.db       (all users, accounts, requests, journal entries)"
Write-Host "  /data/uploads/*           (all uploaded invoice/attachment files)"
Write-Host "  /data/journal_learning/*  (all uploaded journal learning files)"
Write-Host ""
$confirm = Read-Host "Type YES to continue"
if ($confirm -ne "YES") {
    Write-Host "Cancelled." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[INFO] Connecting to $app and wiping /data ..." -ForegroundColor Cyan

$cmd = "rm -f /data/accounting.db /data/accounting.db-wal /data/accounting.db-shm; rm -rf /data/uploads; rm -rf /data/journal_learning; mkdir -p /data/uploads/request_attachments; mkdir -p /data/journal_learning; echo DONE; ls -la /data/"

& $flyctl ssh console --app $app --command $cmd

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "[OK] Data wiped. The app will start completely fresh." -ForegroundColor Green
    Write-Host "     Open the app and create your first admin user via the setup page." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "[ERROR] SSH failed. Make sure you are logged in: flyctl auth login" -ForegroundColor Red
}

Write-Host ""
Write-Host "Press any key to continue . . ."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
