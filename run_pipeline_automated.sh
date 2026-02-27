#!/bin/bash

# --- Pfade ---
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
VENV_PATH="$SCRIPT_DIR/.venv/bin/activate"
SETTINGS_FILE="$SCRIPT_DIR/settings.json"
OPTIMIZER="src/ltbbot/analysis/optimizer.py"
CACHE_DIR="$SCRIPT_DIR/data/cache"
TIMESTAMP_FILE="$CACHE_DIR/.last_cleaned"

# --- Umgebung aktivieren ---
source "$VENV_PATH"

echo "--- Starte automatischen Pipeline-Lauf (ltbbot Envelope) ---"

# --- Python-Helper zum sicheren Auslesen der JSON-Datei ---
get_setting() {
    python3 -c "import json; f=open('$SETTINGS_FILE'); print(json.load(f)$1); f.close()" 2>/dev/null
}

# --- Automatisches Cache-Management ---
CACHE_DAYS=$(get_setting "['optimization_settings']['auto_clear_cache_days']")

if [[ "$CACHE_DAYS" =~ ^[0-9]+$ ]] && [ "$CACHE_DAYS" -gt 0 ]; then
    mkdir -p "$CACHE_DIR"
    if [ ! -f "$TIMESTAMP_FILE" ]; then touch "$TIMESTAMP_FILE"; fi
    if [ -n "$(find "$TIMESTAMP_FILE" -mtime +$((CACHE_DAYS - 1)))" ]; then
        echo "Cache ist älter als $CACHE_DAYS Tage. Leere den Cache..."
        rm -rf "$CACHE_DIR"/*
        touch "$TIMESTAMP_FILE"
    else
        echo "Cache ist aktuell. Keine Reinigung notwendig."
    fi
fi

# --- Prüfen ob Optimierung aktiviert ---
ENABLED=$(get_setting "['optimization_settings']['enabled']")
if [ "$ENABLED" != "True" ]; then
    echo "Automatische Optimierung ist in settings.json deaktiviert. Breche ab."
    deactivate
    exit 2
fi

# --- Einstellungen lesen ---
START_CAPITAL=$(get_setting "['optimization_settings']['start_capital']")
N_CORES=$(get_setting "['optimization_settings']['cpu_cores']")
N_TRIALS=$(get_setting "['optimization_settings']['num_trials']")
MAX_DD=$(get_setting "['optimization_settings']['constraints']['max_drawdown_pct']")
MIN_WR=$(get_setting "['optimization_settings']['constraints']['min_win_rate_pct']")
MIN_PNL=$(get_setting "['optimization_settings']['constraints']['min_pnl_pct']")
MIN_TRADES=$(get_setting "['optimization_settings']['constraints']['min_trades']")
OPTIM_MODE=$(get_setting "['optimization_settings']['mode']")
CONFIG_SUFFIX=$(get_setting "['optimization_settings']['config_suffix']")
LOOKBACK_SETTING=$(get_setting "['optimization_settings']['lookback_days']")
SYMBOLS_SETTING=$(get_setting "['optimization_settings']['symbols_to_optimize']")
TIMEFRAMES_SETTING=$(get_setting "['optimization_settings']['timeframes_to_optimize']")
END_DATE=$(date +%F)

# --- Paare auflösen (auto oder explizit) ---
if [ "$SYMBOLS_SETTING" == "auto" ] || [ "$TIMEFRAMES_SETTING" == "auto" ]; then
    PAIRS=$(python3 -c "
import json
with open('$SETTINGS_FILE') as f:
    s = json.load(f)
seen = set()
pairs = []
for strat in s.get('live_trading_settings', {}).get('active_strategies', []):
    if not strat.get('active', True):
        continue
    sym = strat.get('symbol', '').split('/')[0]
    tf  = strat.get('timeframe', '')
    if sym and tf and (sym, tf) not in seen:
        pairs.append(f'{sym}|{tf}')
        seen.add((sym, tf))
print(' '.join(pairs) if pairs else 'BTC|4h')
")
else
    SYMBOLS=$(echo "$SYMBOLS_SETTING" | tr -d "[]'\" " | tr ',' ' ')
    TIMEFRAMES=$(echo "$TIMEFRAMES_SETTING" | tr -d "[]'\" " | tr ',' ' ')
    PAIRS=""
    for sym in $SYMBOLS; do
        for tf in $TIMEFRAMES; do
            PAIRS="$PAIRS ${sym}|${tf}"
        done
    done
    PAIRS=$(echo "$PAIRS" | xargs)
fi

if [ -z "$PAIRS" ]; then
    echo "Fehler: Keine Paare konfiguriert. Breche ab."
    deactivate
    exit 1
fi

echo "Verwende Paare: $PAIRS"
echo "Optimierung ist aktiviert. Starte Prozesse..."
echo ""

# --- Pipeline pro Paar ausführen (nur Optimizer, kein Trainer/Threshold) ---
for PAIR in $PAIRS; do
    SYM="${PAIR%%|*}"
    TF="${PAIR##*|}"

    # Lookback je Timeframe berechnen
    if [ "$LOOKBACK_SETTING" == "auto" ]; then
        case "$TF" in
            5m|15m)  LB=60   ;;
            30m|1h)  LB=365  ;;
            2h|4h)   LB=730  ;;
            *)        LB=1095 ;;
        esac
    else
        LB="$LOOKBACK_SETTING"
    fi
    START_DATE=$(date -d "$LB days ago" +%F)

    echo "======================================================="
    echo "  Pipeline: $SYM ($TF) | $START_DATE bis $END_DATE"
    echo "======================================================="

    echo ">>> Starte Hyperparameter-Optimierung..."
    python3 "$OPTIMIZER" \
        --symbols       "$SYM" \
        --timeframes    "$TF" \
        --start_date    "$START_DATE" \
        --end_date      "$END_DATE" \
        --jobs          "$N_CORES" \
        --max_drawdown  "$MAX_DD" \
        --start_capital "$START_CAPITAL" \
        --min_win_rate  "$MIN_WR" \
        --trials        "$N_TRIALS" \
        --min_pnl       "$MIN_PNL" \
        --mode          "$OPTIM_MODE" \
        --config_suffix "$CONFIG_SUFFIX" \
        --min_trades    "$MIN_TRADES"

    if [ $? -ne 0 ]; then
        echo "Fehler im Optimierer für $SYM ($TF)."
    else
        echo "✔ Optimierung für $SYM ($TF) abgeschlossen."
    fi
    echo ""
done

deactivate
echo "--- Automatischer Pipeline-Lauf abgeschlossen ---"
