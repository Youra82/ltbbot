#!/bin/bash
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# --- Pfade definieren ---
VENV_PATH=".venv/bin/activate"
PYTHON=".venv/bin/python"
OPTIMIZER="src/ltbbot/analysis/optimizer.py"

# --- Umgebung aktivieren ---
source "$VENV_PATH"
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert.${NC}"

echo ""
echo -e "${BLUE}=======================================================${NC}"
echo "      ltbbot Envelope Optimierungs-Pipeline"
echo -e "${BLUE}=======================================================${NC}"

# --- AUFRÄUM-ASSISTENT ---
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

# --- Paare und Zeitfenster ---
echo ""
read -p "Handelspaar(e) eingeben (ohne /USDT, z.B. BTC ETH DOGE) [leer=auto aus settings.json]: " SYMBOLS
read -p "Zeitfenster eingeben (z.B. 1h 4h) [leer=auto aus settings.json]: " TIMEFRAMES

# Auto aus settings.json falls leer
if [ -z "$SYMBOLS" ]; then
    SYMBOLS=$("$PYTHON" -c "
import json
s = json.load(open('settings.json'))
pairs = s.get('optimization_settings', {}).get('candidate_strategies', [])
syms = list(dict.fromkeys(p['symbol'].split('/')[0] for p in pairs))
print(' '.join(syms))
" 2>/dev/null)
    echo -e "  ${BLUE}Auto-Paare aus settings.json: $SYMBOLS${NC}"
fi
if [ -z "$TIMEFRAMES" ]; then
    TIMEFRAMES=$("$PYTHON" -c "
import json
s = json.load(open('settings.json'))
pairs = s.get('optimization_settings', {}).get('candidate_strategies', [])
tfs = list(dict.fromkeys(p['timeframe'] for p in pairs))
print(' '.join(tfs))
" 2>/dev/null)
    echo -e "  ${BLUE}Auto-Zeitfenster aus settings.json: $TIMEFRAMES${NC}"
fi

# --- OOS-SPLIT ---
echo ""
echo -e "${BLUE}=======================================================${NC}"
echo "  Walk-Forward Out-of-Sample Test (optional)"
echo -e "${BLUE}=======================================================${NC}"
echo ""
echo "  Konzept:"
echo "    1. Du wählst ein OOS-Datum (z.B. 2026-01-01)"
echo "    2. Optimizer trainiert NUR auf Daten VOR diesem Datum"
echo "    3. Die Daten AB diesem Datum sind komplett verborgen"
echo "       — niemals von run_pipeline.sh genutzt"
echo "    4. run_analysis.sh kann die OOS-Daten auswerten"
echo ""
read -p "OOS-Startdatum eingeben [leer=kein OOS-Test, Standard-Modus]: " OOS_INPUT

OOS_START=""
TODAY=$(date +%F)

if [ -n "$OOS_INPUT" ]; then
    OOS_START="$OOS_INPUT"
    TRAIN_END=$(date -d "$OOS_START - 1 day" +%F)
    OOS_DAYS=$(( ($(date +%s) - $(date -d "$OOS_START" +%s)) / 86400 ))

    # Automatisches Startdatum basierend auf Zeitfenster, rückwärts ab OOS-Datum
    _AUTO_LOOKBACK=730
    for tf in $TIMEFRAMES; do
        case "$tf" in
            5m|15m) _AUTO_LOOKBACK=90 ;;
            30m|1h) _AUTO_LOOKBACK=548 ;;
            2h)     _AUTO_LOOKBACK=730 ;;
            4h|6h)  _AUTO_LOOKBACK=1095 ;;
            1d)     _AUTO_LOOKBACK=1825 ;;
        esac
        break
    done
    _AUTO_START=$(date -d "$OOS_START - $_AUTO_LOOKBACK days" +%F)

    echo ""
    echo -e "${GREEN}✔ OOS-Modus aktiv:${NC}"
    echo ""
    printf "  Trainingsperiode:  %-12s ──────────────────────► %s\n" "$_AUTO_START" "$TRAIN_END"
    printf "  Backtestperiode:   %-12s ──────────────────────► %-12s  (dunkler Bereich)\n" "$OOS_START" "$TODAY"
    echo ""
    echo "  ────────────────────────────────────────────────────────────────────"
    printf "  ◄──── TRAINING (%d Tage) ────►  ◄── OOS (%d Tage) ──►\n" "$_AUTO_LOOKBACK" "$OOS_DAYS"
    printf "  %-24s %-16s  %-12s  %s\n" "$_AUTO_START" "$TRAIN_END" "$OOS_START" "$TODAY"
    echo "  ────────────────────────────────────────────────────────────────────"
    echo ""
    echo "  (Startdatum kann unten angepasst werden — 'a' = Automatik = $_AUTO_START)"
    echo ""

    # OOS-Datum in settings.json speichern
    "$PYTHON" -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['oos_start_date'] = '${OOS_START}'
s['optimization_settings']['_oos_note'] = 'Pipeline trainiert NUR auf Daten vor diesem Datum.'
json.dump(s, open('settings.json', 'w'), indent=4)
"
else
    echo -e "${GREEN}✔ Kein OOS — kompletter Zeitraum wird genutzt.${NC}"
    "$PYTHON" -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['oos_start_date'] = None
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null || true
fi

# --- Empfehlung Rückblick ---
echo -e "${BLUE}--- Empfehlung: Optimaler Rückblick-Zeitraum ---${NC}"
printf "+------------------+------------------------------+\n"
printf "| Zeitfenster      | Empfohlener Rückblick (Tage) |\n"
printf "+------------------+------------------------------+\n"
printf "| 5m, 15m          | 90 Tage   (~3 Monate)        |\n"
printf "| 30m, 1h          | 548 Tage  (~1,5 Jahre)       |\n"
printf "| 2h               | 730 Tage  (~2 Jahre)         |\n"
printf "| 4h, 6h           | 1095 Tage (~3 Jahre)         |\n"
printf "| 1d               | 1825 Tage (~5 Jahre)         |\n"
printf "+------------------+------------------------------+\n"
if [ -n "$OOS_START" ]; then
    echo "  Rückblick wird rückwärts ab OOS-Datum ($OOS_START) berechnet"
fi
echo ""
read -p "Startdatum (JJJJ-MM-TT) oder 'a' für Automatik [Standard: a]: " START_DATE_INPUT
START_DATE_INPUT=${START_DATE_INPUT:-a}

# Bestätigung des Zeitraums anzeigen
if [ -n "$OOS_START" ]; then
    if [ "$START_DATE_INPUT" == "a" ]; then
        _SHOW_START="$_AUTO_START"
    else
        _SHOW_START="$START_DATE_INPUT"
    fi
    echo ""
    echo "  Trainingsperiode:  $_SHOW_START  →  $TRAIN_END"
    echo "  OOS-Periode:       ab $OOS_START  →  $TODAY  (komplett verborgen)"
    echo ""
fi

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

# --- Schleife ---
for symbol in $SYMBOLS; do
    for timeframe in $TIMEFRAMES; do
        # Startdatum berechnen
        if [ "$START_DATE_INPUT" == "a" ]; then
            lookback_days=730
            case "$timeframe" in
                5m|15m) lookback_days=90 ;;
                30m|1h) lookback_days=548 ;;
                2h)     lookback_days=730 ;;
                4h|6h)  lookback_days=1095 ;;
                1d)     lookback_days=1825 ;;
            esac
            if [ -n "$OOS_START" ]; then
                CURRENT_START_DATE=$(date -d "$OOS_START - $lookback_days days" +%F)
            else
                CURRENT_START_DATE=$(date -d "$lookback_days days ago" +%F)
            fi
        else
            CURRENT_START_DATE="$START_DATE_INPUT"
        fi

        # OOS: Enddatum kappen
        if [ -n "$OOS_START" ]; then
            CURRENT_END_DATE=$(date -d "$OOS_START - 1 day" +%F)
        else
            CURRENT_END_DATE="$TODAY"
        fi

        echo ""
        echo -e "${BLUE}=======================================================${NC}"
        echo -e "${BLUE}  Bearbeite Pipeline für: $symbol ($timeframe)${NC}"
        echo -e "${BLUE}  Trainingszeitraum: $CURRENT_START_DATE bis $CURRENT_END_DATE${NC}"
        if [ -n "$OOS_START" ]; then
            echo -e "${YELLOW}  OOS-Periode:       ab $OOS_START  →  $TODAY  (verborgen)${NC}"
        fi
        echo -e "${BLUE}=======================================================${NC}"

        echo -e "\n${GREEN}>>> Starte Optimierung für $symbol ($timeframe)...${NC}"
        "$PYTHON" "$OPTIMIZER" \
            --symbols      "$symbol" \
            --timeframes   "$timeframe" \
            --start_date   "$CURRENT_START_DATE" \
            --end_date     "$CURRENT_END_DATE" \
            --jobs         "$N_CORES" \
            --max_drawdown "$MAX_DD" \
            --start_capital "$START_CAPITAL" \
            --min_win_rate "$MIN_WR" \
            --trials       "$N_TRIALS" \
            --min_pnl      "$MIN_PNL" \
            --mode         "$OPTIM_MODE_ARG" \
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
