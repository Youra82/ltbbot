#!/bin/bash
set -e

echo "--- Sicheres Update wird ausgeführt (Robuste Version) ---"

# 1. Sichere lokale Dateien die nicht von Git verwaltet werden sollen
echo "1. Erstelle ein Backup von 'secret.json' und lokalen settings.json-Overrides..."
cp secret.json secret.json.bak

# Sichere Analyse-Ergebnisse aus settings.json (werden durch git reset überschrieben)
SAVED_LB=""
SAVED_OOS=""
if [ -f settings.json ]; then
    SAVED_LB=$(python3 -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('backtest_lookback_weeks',''))" 2>/dev/null || true)
    SAVED_OOS=$(python3 -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('oos_reference_date','') or '')" 2>/dev/null || true)
fi

# 2. Hole die neuesten Daten von GitHub
echo "2. Hole den neuesten Stand von GitHub..."
git fetch origin

# 3. Setze das lokale Verzeichnis hart auf den Stand von GitHub zurück
echo "3. Setze alle Dateien auf den neuesten Stand zurück und verwerfe lokale Änderungen..."
git reset --hard origin/main

# 4. Stelle die API-Schlüssel aus dem Backup wieder her
echo "4. Stelle den Inhalt von 'secret.json' aus dem Backup wieder her..."
cp secret.json.bak secret.json
rm secret.json.bak

# Stelle Analyse-Ergebnisse in settings.json wieder her (falls vorhanden)
if [ -n "$SAVED_LB" ] && [ "$SAVED_LB" != "" ]; then
    python3 -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['backtest_lookback_weeks'] = int('$SAVED_LB')
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null && echo "   ✅ backtest_lookback_weeks=$SAVED_LB wiederhergestellt." || true
fi
if [ -n "$SAVED_OOS" ] && [ "$SAVED_OOS" != "None" ]; then
    python3 -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['oos_reference_date'] = '$SAVED_OOS'
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null && echo "   ✅ oos_reference_date=$SAVED_OOS wiederhergestellt." || true
fi

# 5. Lösche den Python-Cache, um alte Code-Versionen zu entfernen
echo "5. Lösche alten Python-Cache für einen sauberen Neustart..."
find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -delete

# 6. Setze die Ausführungsrechte für alle Skripte
echo "6. Setze Ausführungsrechte für alle .sh-Skripte..."
chmod +x *.sh

# 7. venv-Gesundheitscheck — Rebuild falls pip kaputt ist
echo "7. Prüfe venv-Gesundheit..."
VENV_OK=true
if [ ! -f ".venv/bin/python3" ]; then
    echo "   venv fehlt — wird neu erstellt..."
    VENV_OK=false
elif ! .venv/bin/python3 -c "import pip" 2>/dev/null; then
    echo "   pip nicht importierbar — venv wird neu erstellt..."
    VENV_OK=false
elif ! .venv/bin/pip --version 2>/dev/null | grep -q pip; then
    echo "   pip defekt — venv wird neu erstellt..."
    VENV_OK=false
elif ! .venv/bin/python3 -c "from pip._vendor.resolvelib.structs import RequirementInformation" 2>/dev/null; then
    echo "   pip-Resolver defekt — venv wird neu erstellt..."
    VENV_OK=false
fi

if [ "$VENV_OK" = false ]; then
    rm -rf .venv
    python3 -m venv .venv
    echo "   Installiere Dependencies..."
    .venv/bin/pip install --quiet -r requirements.txt
    echo "   ✅ venv neu erstellt und Dependencies installiert."
else
    echo "   ✅ venv ist gesund."
fi

echo "✅ Update erfolgreich abgeschlossen. Dein Bot ist jetzt auf dem neuesten Stand."
