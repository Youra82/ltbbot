# Quick Test Script für LTBBOT v2.0 (Windows PowerShell)
# Testet die neuen Features mit einem einzelnen Trading-Zyklus

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "LTBBOT v2.0 - Quick Test Script" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# Prüfe ob .venv existiert
if (Test-Path ".venv") {
    Write-Host "✓ Aktiviere virtuelle Umgebung..." -ForegroundColor Green
    & ".venv\Scripts\Activate.ps1"
} else {
    Write-Host "✗ Fehler: .venv nicht gefunden!" -ForegroundColor Red
    exit 1
}

# Prüfe Python
try {
    $pythonVersion = & python --version 2>&1
    Write-Host "✓ Python gefunden: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "✗ Fehler: Python nicht gefunden!" -ForegroundColor Red
    exit 1
}
Write-Host ""

# Prüfe Dependencies
Write-Host "Prüfe Dependencies..." -ForegroundColor Yellow
try {
    & python -c "import pandas, ta, ccxt" 2>$null
    Write-Host "✓ Alle Dependencies installiert" -ForegroundColor Green
} catch {
    Write-Host "⚠ Installiere fehlende Dependencies..." -ForegroundColor Yellow
    & pip install -r requirements.txt
}
Write-Host ""

# Erstelle Backup
Write-Host "Erstelle Backup der Tracker-Dateien..." -ForegroundColor Yellow
if (Test-Path "artifacts\tracker") {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupDir = "artifacts\tracker_backup_$timestamp"
    Copy-Item -Path "artifacts\tracker" -Destination $backupDir -Recurse
    Write-Host "✓ Backup erstellt: $backupDir" -ForegroundColor Green
} else {
    Write-Host "⚠ Kein Tracker-Verzeichnis gefunden (normal bei Neuinstallation)" -ForegroundColor Yellow
}
Write-Host ""

# Test-Lauf Info
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "STARTE TEST-LAUF MIT BTC/USDT:USDT (4h)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Dies ist ein einzelner Zyklus zum Testen der neuen Features." -ForegroundColor White
Write-Host "Der Bot wird:" -ForegroundColor White
Write-Host "  - Marktregime analysieren" -ForegroundColor Gray
Write-Host "  - Performance prüfen" -ForegroundColor Gray
Write-Host "  - Indikatoren berechnen" -ForegroundColor Gray
Write-Host "  - Ggf. Orders platzieren (wenn Bedingungen erfüllt)" -ForegroundColor Gray
Write-Host ""

$confirmation = Read-Host "Fortfahren? (y/n)"
if ($confirmation -ne 'y' -and $confirmation -ne 'Y') {
    Write-Host "Abgebrochen." -ForegroundColor Yellow
    exit 0
}

# Führe Test aus
Write-Host ""
Write-Host "Starte Test-Lauf..." -ForegroundColor Cyan
& python src/ltbbot/strategy/run.py --symbol "BTC/USDT:USDT" --timeframe "4h"

# Prüfe Exit-Code
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "✓ TEST ERFOLGREICH ABGESCHLOSSEN" -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Nächste Schritte:" -ForegroundColor White
    Write-Host "1. Prüfe Logs in: logs\ltbbot_BTCUSDTUSDT_4h.log" -ForegroundColor Gray
    Write-Host "2. Prüfe Tracker: artifacts\tracker\BTC-USDT-USDT_4h.json" -ForegroundColor Gray
    Write-Host "3. Bei Erfolg: Starte master_runner.py für automatischen Betrieb" -ForegroundColor Gray
    Write-Host ""
    Write-Host "Automatischer Betrieb:" -ForegroundColor Yellow
    Write-Host "  python master_runner.py" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Log-Monitoring (Echtzeit):" -ForegroundColor Yellow
    Write-Host "  Get-Content -Wait logs\ltbbot_BTCUSDTUSDT_4h.log" -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Red
    Write-Host "✗ FEHLER BEIM TEST" -ForegroundColor Red
    Write-Host "========================================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "Prüfe die Logs für Details:" -ForegroundColor Yellow
    Write-Host "  Get-Content logs\ltbbot_BTCUSDTUSDT_4h.log -Tail 50" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}
