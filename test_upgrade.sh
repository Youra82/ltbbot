#!/bin/bash
# Quick Test Script für LTBBOT v2.0
# Testet die neuen Features mit einem einzelnen Trading-Zyklus

echo "========================================================"
echo "LTBBOT v2.0 - Quick Test Script"
echo "========================================================"
echo ""

# Aktiviere virtuelle Umgebung
if [ -d ".venv" ]; then
    echo "✓ Aktiviere virtuelle Umgebung..."
    source .venv/bin/activate
else
    echo "✗ Fehler: .venv nicht gefunden!"
    exit 1
fi

# Prüfe ob Python verfügbar ist
if ! command -v python3 &> /dev/null; then
    echo "✗ Fehler: Python3 nicht gefunden!"
    exit 1
fi

echo "✓ Python gefunden: $(python3 --version)"
echo ""

# Prüfe Dependencies
echo "Prüfe Dependencies..."
python3 -c "import pandas, ta, ccxt" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "✓ Alle Dependencies installiert"
else
    echo "⚠ Installiere fehlende Dependencies..."
    pip install -r requirements.txt
fi
echo ""

# Erstelle Backup der Tracker-Dateien
echo "Erstelle Backup der Tracker-Dateien..."
if [ -d "artifacts/tracker" ]; then
    BACKUP_DIR="artifacts/tracker_backup_$(date +%Y%m%d_%H%M%S)"
    cp -r artifacts/tracker "$BACKUP_DIR"
    echo "✓ Backup erstellt: $BACKUP_DIR"
else
    echo "⚠ Kein Tracker-Verzeichnis gefunden (normal bei Neuinstallation)"
fi
echo ""

# Test-Lauf mit BTC
echo "========================================================"
echo "STARTE TEST-LAUF MIT BTC/USDT:USDT (4h)"
echo "========================================================"
echo ""
echo "Dies ist ein einzelner Zyklus zum Testen der neuen Features."
echo "Der Bot wird:"
echo "  - Marktregime analysieren"
echo "  - Performance prüfen"
echo "  - Indikatoren berechnen"
echo "  - Ggf. Orders platzieren (wenn Bedingungen erfüllt)"
echo ""
read -p "Fortfahren? (y/n): " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Abgebrochen."
    exit 0
fi

# Führe Test aus
python3 src/ltbbot/strategy/run.py --symbol "BTC/USDT:USDT" --timeframe "4h"

# Prüfe Exit-Code
if [ $? -eq 0 ]; then
    echo ""
    echo "========================================================"
    echo "✓ TEST ERFOLGREICH ABGESCHLOSSEN"
    echo "========================================================"
    echo ""
    echo "Nächste Schritte:"
    echo "1. Prüfe Logs in: logs/ltbbot_BTCUSDTUSDT_4h.log"
    echo "2. Prüfe Tracker: artifacts/tracker/BTC-USDT-USDT_4h.json"
    echo "3. Bei Erfolg: Starte master_runner.py für automatischen Betrieb"
    echo ""
    echo "Automatischer Betrieb:"
    echo "  python3 master_runner.py"
    echo ""
else
    echo ""
    echo "========================================================"
    echo "✗ FEHLER BEIM TEST"
    echo "========================================================"
    echo ""
    echo "Prüfe die Logs für Details:"
    echo "  cat logs/ltbbot_BTCUSDTUSDT_4h.log | tail -50"
    echo ""
    exit 1
fi
