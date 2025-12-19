#!/bin/bash

# --- Skript zum Senden von Dateien an Telegram ---
# Verwendung: bash send_report.sh <Dateiname>
# Beispiel:   bash send_report.sh optimal_portfolio_equity.csv

# Überprüfen, ob ein Dateiname übergeben wurde
if [ -z "$1" ]; then
    echo "Fehler: Du musst einen Dateinamen als Argument übergeben."
    echo "Beispiel: bash send_report.sh optimal_portfolio_equity.csv"
    exit 1
fi

FILENAME=$1
# Bestimme das Projektverzeichnis dynamisch
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
FILE_PATH="$PROJECT_ROOT/$FILENAME"

# Überprüfen, ob die Datei existiert
if [ ! -f "$FILE_PATH" ]; then
    echo "Fehler: Die Datei '$FILE_PATH' wurde nicht gefunden."
    exit 1
fi

echo "Lese API-Daten aus secret.json..."
# Prüfen ob jq installiert ist
if ! command -v jq &> /dev/null
then
    echo "Fehler: 'jq' ist nicht installiert. Bitte installieren (sudo apt install jq)."
    exit 1
fi

SECRET_FILE="$PROJECT_ROOT/secret.json"
if [ ! -f "$SECRET_FILE" ]; then
    echo "Fehler: secret.json nicht gefunden in $PROJECT_ROOT."
    exit 1
fi

# Lese Token und Chat ID sicher aus
BOT_TOKEN=$(jq -r '.telegram.bot_token // empty' "$SECRET_FILE")
CHAT_ID=$(jq -r '.telegram.chat_id // empty' "$SECRET_FILE")

if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
    echo "Fehler: bot_token oder chat_id nicht in secret.json gefunden oder leer."
    exit 1
fi

# Eine passende Beschreibung erstellen
CAPTION="ltbbot Backtest-Bericht für '$FILENAME' vom $(date +'%Y-%m-%d %H:%M:%S')" # Botname angepasst

echo "Sende '$FILENAME' an Telegram..."

# Datei mit curl an die Telegram API senden
# -s für silent, -X POST, -F für Formular-Daten
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" \
     -F "chat_id=$CHAT_ID" \
     -F "document=@$FILE_PATH" \
     -F "caption=$CAPTION" > /dev/null # Ausgabe unterdrücken

# Einfache Erfolgsprüfung (curl gibt bei Fehler >0 zurück)
if [ $? -eq 0 ]; then
    echo "✔ Datei wurde erfolgreich an Telegram gesendet!"
else
    echo "❌ Fehler beim Senden an Telegram via curl."
fi
