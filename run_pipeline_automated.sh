#!/bin/bash

# --- Pfade ---
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
VENV_PATH="$SCRIPT_DIR/.venv/bin/activate"
SETTINGS_FILE="$SCRIPT_DIR/settings.json"
PORTFOLIO_RUNNER="src/ltbbot/analysis/show_results.py"
CACHE_DIR="$SCRIPT_DIR/data/cache"
TIMESTAMP_FILE="$CACHE_DIR/.last_cleaned"

# --- Umgebung aktivieren ---
if [ ! -f "$VENV_PATH" ]; then
    echo "Fehler: Virtuelle Umgebung nicht gefunden unter $VENV_PATH."
    exit 1
fi
source "$VENV_PATH"

echo "--- Starte automatischen Portfolio-Optimierungs-Lauf (ltbbot Envelope) ---"

# --- settings.json prüfen ---
if [ ! -f "$SETTINGS_FILE" ]; then
    echo "Fehler: settings.json nicht gefunden."
    deactivate; exit 1
fi

# --- Python-Helper zum sicheren Auslesen der JSON-Datei ---
get_setting() {
    python3 -c "import json; from functools import reduce; d=json.load(open('$SETTINGS_FILE')); v=reduce(lambda a,k: a.get(k) if isinstance(a,dict) else None, $1, d); print('' if v is None else v)" 2>/dev/null
}

# --- Automatisches Cache-Management ---
CACHE_DAYS=$(get_setting "['optimization_settings', 'auto_clear_cache_days']")
CACHE_DAYS=${CACHE_DAYS:-0}

if [[ "$CACHE_DAYS" =~ ^[0-9]+$ ]] && [ "$CACHE_DAYS" -gt 0 ]; then
    mkdir -p "$CACHE_DIR"
    if [ ! -f "$TIMESTAMP_FILE" ]; then touch "$TIMESTAMP_FILE"; fi
    if find "$TIMESTAMP_FILE" -mtime +$((CACHE_DAYS - 1)) -print -quit | grep -q .; then
        echo "Cache ist älter als $CACHE_DAYS Tage. Leere den Cache..."
        rm -rf "$CACHE_DIR"/*
        touch "$TIMESTAMP_FILE"
    else
        echo "Cache ist aktuell. Keine Reinigung notwendig."
    fi
else
    echo "Automatisches Cache-Management deaktiviert."
fi

# --- Prüfen ob Portfolio-Optimierung aktiviert ---
if command -v jq &> /dev/null; then
    ENABLED=$(jq -r '.optimization_settings.enabled // false' "$SETTINGS_FILE")
else
    ENABLED=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('optimization_settings',{}).get('enabled', False))")
fi
ENABLED=${ENABLED:-false}
ENABLED_LC=$(echo "$ENABLED" | tr '[:upper:]' '[:lower:]')

if [ "$ENABLED_LC" != "true" ]; then
    echo "Automatische Portfolio-Optimierung ist in settings.json deaktiviert. Breche ab."
    deactivate; exit 0
fi

# --- Alle vorhandenen Configs zählen ---
N_CONFIGS=$(ls src/ltbbot/strategy/configs/config_*_envelope.json 2>/dev/null | wc -l)
if [ "$N_CONFIGS" -eq 0 ]; then
    echo "⚠  Keine Configs in src/ltbbot/strategy/configs/ gefunden."
    echo "   Bitte zuerst run_pipeline.sh ausführen um Configs zu generieren."
    deactivate; exit 1
fi

echo ""
echo "======================================================="
echo "  Wöchentliche Auto-Portfolio-Optimierung"
echo "  Configs gefunden: $N_CONFIGS"
echo "  Zeitraum: wird aus settings.json (backtest_lookback_weeks) gelesen"
echo "  Ergebnis: automatisch in settings.json active_strategies"
echo "======================================================="
echo ""

echo ">>> Starte Portfolio-Optimierung über ALLE $N_CONFIGS Config(s)..."
python3 -u "$PORTFOLIO_RUNNER" --mode 3 --auto

EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo "⚠  Fehler in der Portfolio-Optimierung (Exit $EXIT_CODE)."
    deactivate; exit 1
fi

deactivate
echo ""
echo "--- Automatischer Portfolio-Optimierungs-Lauf abgeschlossen ---"
