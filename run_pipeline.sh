#!/bin/bash
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

VENV_PATH=".venv/bin/activate"
PYTHON=".venv/bin/python"
OPTIMIZER="src/ltbbot/analysis/optimizer.py"
TODAY=$(date +%F)

source "$VENV_PATH"
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert.${NC}"

echo ""
echo -e "${BLUE}=======================================================${NC}"
echo "      ltbbot Envelope Optimierungs-Pipeline"
echo -e "${BLUE}=======================================================${NC}"

# --- Aufräumen ---
echo ""
echo -e "${YELLOW}Möchtest du alle alten, generierten Configs vor dem Start löschen?${NC}"
read -p "Dies wird für einen kompletten Neustart empfohlen. (j/n) [Standard: n]: " CLEANUP_CHOICE
CLEANUP_CHOICE=${CLEANUP_CHOICE:-n}
if [[ "$CLEANUP_CHOICE" == "j" || "$CLEANUP_CHOICE" == "J" ]]; then
    rm -f src/ltbbot/strategy/configs/config_*_envelope.json
    rm -f artifacts/results/last_optimizer_run.json
    rm -rf data/cache/
    echo -e "${GREEN}✔ Kompletter Neustart — Configs, Optimizer-Ergebnis und Cache gelöscht.${NC}"
else
    echo -e "${GREEN}✔ Alte Configs werden beibehalten.${NC}"
fi

# --- Paare & Zeitfenster ---
echo ""
read -p "Handelspaar(e) eingeben (ohne /USDT, z.B. BTC ETH DOGE) [leer=auto]: " SYMBOLS
read -p "Zeitfenster eingeben (z.B. 1h 4h) [leer=auto]: " TIMEFRAMES

if [ -z "$SYMBOLS" ]; then
    SYMBOLS=$("$PYTHON" -c "
import json
s = json.load(open('settings.json'))
pairs = s.get('optimization_settings', {}).get('candidate_strategies', [])
print(' '.join(dict.fromkeys(p['symbol'].split('/')[0] for p in pairs)))
" 2>/dev/null)
    echo -e "  ${BLUE}Auto-Paare: $SYMBOLS${NC}"
fi
if [ -z "$TIMEFRAMES" ]; then
    TIMEFRAMES=$("$PYTHON" -c "
import json
s = json.load(open('settings.json'))
pairs = s.get('optimization_settings', {}).get('candidate_strategies', [])
print(' '.join(dict.fromkeys(p['timeframe'] for p in pairs)))
" 2>/dev/null)
    echo -e "  ${BLUE}Auto-Zeitfenster: $TIMEFRAMES${NC}"
fi

# --- OOS-SPLIT ---
echo ""
echo -e "${BLUE}=======================================================${NC}"
echo "  Walk-Forward Out-of-Sample Test (optional)"
echo -e "${BLUE}=======================================================${NC}"
echo ""
echo "  Konzept:"
echo "    Optimizer trainiert NUR auf 70% der Daten (je Timeframe)."
echo "    Die restlichen 30% bleiben komplett verborgen."
echo "    Je Timeframe wird der Split automatisch berechnet:"
echo ""
echo "      1d  → 1825 Tage gesamt: 1277 Training + 548 OOS"
echo "      4h  → 1095 Tage gesamt:  766 Training + 329 OOS"
echo "      1h  →  548 Tage gesamt:  383 Training + 165 OOS"
echo "      15m →   90 Tage gesamt:   63 Training +  27 OOS"
echo ""
echo "  Optionen:  'auto' | JJJJ-MM-TT (fixes Datum) | leer=kein OOS"
echo ""
read -p "OOS-Modus eingeben [auto / Datum / leer]: " OOS_INPUT

# OOS_MODE: "auto", "fixed", ""
OOS_MODE=""
OOS_FIXED=""

if [ "$OOS_INPUT" == "auto" ]; then
    OOS_MODE="auto"
    echo -e "${GREEN}✔ OOS-Modus: auto — 70/30 wird pro Timeframe in der Schleife berechnet.${NC}"
    "$PYTHON" -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['oos_start_date'] = 'auto'
s['optimization_settings']['_oos_note'] = '70/30-Split automatisch je Timeframe.'
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null || true
elif [ -n "$OOS_INPUT" ]; then
    OOS_MODE="fixed"
    OOS_FIXED="$OOS_INPUT"
    TRAIN_END_FIXED=$(date -d "$OOS_FIXED - 1 day" +%F)
    OOS_DAYS_FIXED=$(( ($(date +%s) - $(date -d "$OOS_FIXED" +%s)) / 86400 ))
    echo ""
    echo -e "${GREEN}✔ OOS-Modus: fixes Datum $OOS_FIXED${NC}"
    echo ""
    echo "  ────────────────────────────────────────────────────────────────────"
    printf "  ◄──── TRAINING ────►  ◄── OOS (%d Tage, verborgen) ──►\n" "$OOS_DAYS_FIXED"
    printf "  %-28s  %-12s  %s\n" "$TRAIN_END_FIXED" "$OOS_FIXED" "$TODAY"
    echo "  ────────────────────────────────────────────────────────────────────"
    echo ""
    "$PYTHON" -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['oos_start_date'] = '${OOS_FIXED}'
s['optimization_settings']['_oos_note'] = 'Fixes OOS-Datum fuer alle Timeframes.'
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null || true
else
    echo -e "${GREEN}✔ Kein OOS — kompletter Zeitraum wird genutzt.${NC}"
    "$PYTHON" -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['oos_start_date'] = None
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null || true
fi

# --- Startdatum ---
echo ""
echo -e "${BLUE}--- Empfehlung: Optimaler Rückblick-Zeitraum ---${NC}"
printf "+------------------+----------------------------------------------+\n"
printf "| Zeitfenster      | Lookback  | 70%% Training | 30%% OOS           |\n"
printf "+------------------+----------------------------------------------+\n"
printf "| 5m, 15m          |  90 Tage  |  63 Tage      |  27 Tage           |\n"
printf "| 30m, 1h          | 548 Tage  | 383 Tage      | 165 Tage           |\n"
printf "| 2h               | 730 Tage  | 511 Tage      | 219 Tage           |\n"
printf "| 4h, 6h           |1095 Tage  | 766 Tage      | 329 Tage           |\n"
printf "| 1d               |1825 Tage  |1277 Tage      | 548 Tage           |\n"
printf "+------------------+----------------------------------------------+\n"
echo ""
read -p "Startdatum (JJJJ-MM-TT) oder 'a' für Automatik [Standard: a]: " START_DATE_INPUT
START_DATE_INPUT=${START_DATE_INPUT:-a}

read -p "Startkapital in USDT [Standard: 1000]: " START_CAPITAL; START_CAPITAL=${START_CAPITAL:-1000}
read -p "CPU-Kerne [Standard: -1 für alle]: " N_CORES; N_CORES=${N_CORES:--1}
read -p "Anzahl Trials [Standard: 200]: " N_TRIALS; N_TRIALS=${N_TRIALS:-200}

echo ""
echo -e "${YELLOW}Wähle einen Optimierungs-Modus:${NC}"
echo "  1) Strenger Modus    (Profitabel + WR >= Min. Win-Rate + MaxDD <= Limit)"
echo "  2) Best-Profit-Modus (Nur MaxDD-Limit, maximiert PnL)"
read -p "Auswahl (1-2) [Standard: 1]: " OPTIM_MODE_CHOICE; OPTIM_MODE_CHOICE=${OPTIM_MODE_CHOICE:-1}
if [ "$OPTIM_MODE_CHOICE" == "1" ]; then
    OPTIM_MODE_ARG="strict"
    read -p "Max Drawdown % [Standard: 30]: " MAX_DD; MAX_DD=${MAX_DD:-30}
    read -p "Min Win-Rate % [Standard: 0]: " MIN_WR; MIN_WR=${MIN_WR:-0}
    read -p "Min PnL % [Standard: 0]: " MIN_PNL; MIN_PNL=${MIN_PNL:-0}
else
    OPTIM_MODE_ARG="best_profit"
    read -p "Max Drawdown % [Standard: 30]: " MAX_DD; MAX_DD=${MAX_DD:-30}
    MIN_WR=0
    MIN_PNL=-99999
fi

# --- Schleife pro Symbol + Timeframe ---
for symbol in $SYMBOLS; do
    for timeframe in $TIMEFRAMES; do

        # Lookback je Timeframe
        lookback_days=730
        case "$timeframe" in
            5m|15m) lookback_days=90 ;;
            30m|1h) lookback_days=548 ;;
            2h)     lookback_days=730 ;;
            4h|6h)  lookback_days=1095 ;;
            1d)     lookback_days=1825 ;;
        esac

        # OOS-Split pro Timeframe berechnen
        if [ "$OOS_MODE" == "auto" ]; then
            oos_days_tf=$(( lookback_days * 30 / 100 ))
            train_days_tf=$(( lookback_days - oos_days_tf ))
            OOS_START_TF=$(date -d "$oos_days_tf days ago" +%F)
            CURRENT_END_DATE=$(date -d "$OOS_START_TF - 1 day" +%F)
            CURRENT_START_DATE=$(date -d "$lookback_days days ago" +%F)
        elif [ "$OOS_MODE" == "fixed" ]; then
            OOS_START_TF="$OOS_FIXED"
            CURRENT_END_DATE=$(date -d "$OOS_FIXED - 1 day" +%F)
            if [ "$START_DATE_INPUT" == "a" ]; then
                CURRENT_START_DATE=$(date -d "$OOS_FIXED - $lookback_days days" +%F)
            else
                CURRENT_START_DATE="$START_DATE_INPUT"
            fi
        else
            OOS_START_TF=""
            CURRENT_END_DATE="$TODAY"
            if [ "$START_DATE_INPUT" == "a" ]; then
                CURRENT_START_DATE=$(date -d "$lookback_days days ago" +%F)
            else
                CURRENT_START_DATE="$START_DATE_INPUT"
            fi
        fi

        # START_DATE_INPUT überschreibt wenn nicht 'a' und kein 'fixed' OOS
        if [ "$START_DATE_INPUT" != "a" ] && [ "$OOS_MODE" != "fixed" ]; then
            CURRENT_START_DATE="$START_DATE_INPUT"
        fi

        echo ""
        echo -e "${BLUE}=======================================================${NC}"
        echo -e "${BLUE}  Bearbeite Pipeline für: $symbol ($timeframe)${NC}"
        echo -e "${BLUE}  Trainingszeitraum: $CURRENT_START_DATE  →  $CURRENT_END_DATE${NC}"
        if [ -n "$OOS_START_TF" ]; then
            oos_days_show=$(( ($(date +%s) - $(date -d "$OOS_START_TF" +%s)) / 86400 ))
            train_days_show=$(( lookback_days - oos_days_show ))
            echo ""
            echo "  ────────────────────────────────────────────────────────────────"
            printf "  ◄── TRAINING (%d Tage) ──►  ◄── OOS (%d Tage, verborgen) ──►\n" \
                "$train_days_show" "$oos_days_show"
            printf "  %-24s %-14s  %-12s  %s\n" \
                "$CURRENT_START_DATE" "$CURRENT_END_DATE" "$OOS_START_TF" "$TODAY"
            echo "  ────────────────────────────────────────────────────────────────"
        fi
        echo -e "${BLUE}=======================================================${NC}"

        echo -e "\n${GREEN}>>> Starte Optimierung für $symbol ($timeframe)...${NC}"
        "$PYTHON" "$OPTIMIZER" \
            --symbols       "$symbol" \
            --timeframes    "$timeframe" \
            --start_date    "$CURRENT_START_DATE" \
            --end_date      "$CURRENT_END_DATE" \
            --jobs          "$N_CORES" \
            --max_drawdown  "$MAX_DD" \
            --start_capital "$START_CAPITAL" \
            --min_win_rate  "$MIN_WR" \
            --trials        "$N_TRIALS" \
            --min_pnl       "$MIN_PNL" \
            --mode          "$OPTIM_MODE_ARG" \
            --config_suffix "_envelope"

        if [ $? -ne 0 ]; then
            echo -e "${RED}Fehler im Optimierer für $symbol ($timeframe). Überspringe...${NC}"
        else
            echo -e "${GREEN}✔ Optimierung für $symbol ($timeframe) abgeschlossen.${NC}"
        fi
    done
done

echo ""
echo -e "${BLUE}=======================================================${NC}"
echo -e "${BLUE}✔ Alle Optimierungen abgeschlossen!${NC}"
echo -e "${BLUE}=======================================================${NC}"

# --- Settings aktualisieren ---
echo ""
echo -e "${YELLOW}Möchtest du die optimierten Strategien automatisch in settings.json übernehmen?${NC}"
read -p "Settings aktualisieren? (j/n) [Standard: n]: " UPDATE_SETTINGS_CHOICE
UPDATE_SETTINGS_CHOICE=${UPDATE_SETTINGS_CHOICE:-n}

if [[ "$UPDATE_SETTINGS_CHOICE" == "j" || "$UPDATE_SETTINGS_CHOICE" == "J" ]]; then
    echo -e "\n${GREEN}>>> Aktualisiere settings.json...${NC}"
    "$PYTHON" - <<'PYEOF'
import json, os, glob
ROOT = os.path.abspath('.')
settings = json.load(open(os.path.join(ROOT, 'settings.json')))
configs  = glob.glob(os.path.join(ROOT, 'src', 'ltbbot', 'strategy', 'configs', 'config_*_envelope.json'))
if not configs:
    print("⚠  Keine Config-Dateien gefunden.")
    exit(0)
new_strats = []
for f in sorted(configs):
    cfg = json.load(open(f))
    sym = cfg.get('market', {}).get('symbol')
    tf  = cfg.get('market', {}).get('timeframe')
    if sym and tf and not any(s['symbol']==sym and s['timeframe']==tf for s in new_strats):
        new_strats.append({'symbol': sym, 'timeframe': tf, 'active': True})
        print(f"  ✔ {sym} ({tf})")
settings['live_trading_settings']['active_strategies'] = new_strats
settings['live_trading_settings']['use_auto_optimizer_results'] = True
json.dump(settings, open(os.path.join(ROOT, 'settings.json'), 'w'), indent=4)
print(f"\n✅ settings.json aktualisiert — {len(new_strats)} Strategie(n) aktiv.")
PYEOF
else
    echo -e "${GREEN}✔ settings.json wurde NICHT verändert.${NC}"
fi

deactivate
echo ""
echo -e "${BLUE}=======================================================${NC}"
echo -e "${BLUE}✔ Pipeline abgeschlossen!${NC}"
echo -e "${BLUE}=======================================================${NC}"
