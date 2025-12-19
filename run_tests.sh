#!/bin/bash
# Dieses Skript führt das komplette Test-Sicherheitsnetz aus.
echo "--- Starte ltbbot-Sicherheitsnetz ---"

# Aktiviere die virtuelle Umgebung
if [ ! -f ".venv/bin/activate" ]; then
    echo "Fehler: Virtuelle Umgebung nicht gefunden. Bitte install.sh ausführen."
    exit 1
fi
source .venv/bin/activate

# Führe pytest aus. -v für mehr Details, -s um print() Ausgaben anzuzeigen.
echo "Führe Pytest aus (inkl. Live-Workflow-Test)..."
# Stelle sicher, dass Python das tests-Verzeichnis findet
export PYTHONPATH="${PYTHONPATH}:$(pwd)/src"

if python3 -m pytest -v -s; then
    # Exit Code 0: Alle Tests bestanden
    echo "✅ Pytest erfolgreich durchgelaufen. Alle Tests bestanden."
    EXIT_CODE=0
else
    # Anderer Exit Code
    PYTEST_EXIT_CODE=$?
    if [ $PYTEST_EXIT_CODE -eq 5 ]; then
        # Exit Code 5: Keine Tests gefunden
        echo "⚠️ Pytest beendet: Keine Tests zum Ausführen gefunden."
        EXIT_CODE=0 # Betrachte dies nicht als Fehler für das Skript
    else
        # Anderer Fehler (z.B. Tests fehlgeschlagen)
        echo "❌ Pytest fehlgeschlagen (Exit Code: $PYTEST_EXIT_CODE)."
        EXIT_CODE=$PYTEST_EXIT_CODE
    fi
fi

# Deaktiviere die Umgebung wieder
deactivate

echo "--- Sicherheitscheck abgeschlossen ---"
exit $EXIT_CODE # Gib den Pytest-Fehlercode weiter (außer bei Code 5)
