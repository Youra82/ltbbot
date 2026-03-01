#!/bin/bash
set -e

echo "--- Sicheres Update wird ausgeführt (Robuste Version) ---"

# 1. Sichere die einzige Datei, die lokal wichtig ist
echo "1. Erstelle ein Backup von 'secret.json'..."
cp secret.json secret.json.bak

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

# 5. Lösche den Python-Cache, um alte Code-Versionen zu entfernen
echo "5. Lösche alten Python-Cache für einen sauberen Neustart..."
find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -delete

# 6. Setze die Ausführungsrechte für alle Skripte
echo "6. Setze Ausführungsrechte für alle .sh-Skripte..."
chmod +x *.sh

# 7. Python-Bibliotheken aktualisieren (falls requirements.txt geändert wurde)
echo "7. Aktualisiere Python-Bibliotheken..."
# Prüfe ob venv/pip wirklich funktioniert (pip --version reicht nicht, da Resolver nicht geladen wird)
if ! .venv/bin/python3 -c "from pip._vendor.resolvelib import BaseReporter" > /dev/null 2>&1; then
    echo "⚠️  venv/pip ist beschädigt (inkonsistente pip-Pakete). Erstelle venv neu..."
    rm -rf .venv
    python3 -m venv .venv --upgrade-deps
    .venv/bin/pip install --upgrade pip setuptools wheel -q
fi
.venv/bin/pip install --break-system-packages -q -r requirements.txt

echo "✅ Update erfolgreich abgeschlossen. Dein Bot ist jetzt auf dem neuesten Stand."
