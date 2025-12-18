#!/bin/bash

# Überprüfen, ob ein Dateiname übergeben wurde
if [ -z "$1" ]; then
    echo "Fehler: Du musst den Namen der CSV-Datei angeben."
    echo "Beispiel: bash show_chart.sh optimal_portfolio_equity.csv"
    exit 1
fi

# Bestimme das Projektverzeichnis dynamisch
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
VENV_PATH="$PROJECT_ROOT/.venv/bin/activate"
CHART_SCRIPT="$PROJECT_ROOT/generate_and_send_chart.py" # Pfad zum Python Skript

# Prüfen ob venv existiert
if [ ! -f "$VENV_PATH" ]; then
    echo "Fehler: Virtuelle Umgebung nicht gefunden unter $VENV_PATH. Hast du install.sh ausgeführt?"
    exit 1
fi

# Prüfen ob Chart-Skript existiert
if [ ! -f "$CHART_SCRIPT" ]; then
    echo "Fehler: Chart-Skript nicht gefunden unter $CHART_SCRIPT."
    exit 1
fi

# Aktiviere die virtuelle Umgebung
source "$VENV_PATH"

echo "Aktiviere venv und starte Chart-Generierung für $1..."

# Führe das Python-Skript aus und übergebe den Dateinamen
# Wichtig: Stelle sicher, dass das Python Skript im Projekt Root nach der CSV sucht
# oder übergebe den vollen Pfad zur CSV, falls nötig.
python3 "$CHART_SCRIPT" "$1" # Das Python Skript sollte $PROJECT_ROOT/$1 öffnen

# Deaktiviere die Umgebung wieder
deactivate

echo "Chart-Generierung und Senden abgeschlossen."
