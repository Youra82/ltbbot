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
    rm -f artifacts/results/portfolio_optimization_results.json
    rm -f artifacts/db/optuna_studies_ltbbot.db
    rm -rf data/cache/
    # Laufende Hintergrundprozesse des Schedulers beenden
    pkill -f auto_optimizer_scheduler.py 2>/dev/null || true
    echo -e "${GREEN}✔ Kompletter Neustart — Configs, DB, Ergebnisse, Cache gelöscht und Hintergrundprozesse beendet.${NC}"
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

# --- OOS-Split ---
# Der frühere, eigene "Walk-Forward Out-of-Sample"-Mechanismus auf dieser
# Ebene (End-Datum -> 70/30-Split je Timeframe, schnitt VOR dem Optimizer-
# Aufruf schon die juengsten 30% der Historie komplett weg) wurde am
# 2026-08-27 entfernt: seit dem IS/OOS-Port von stbot (2026-08-21) macht
# optimizer.py selbst per --is_fraction/--k_folds/--min_oos_trades einen
# eigenen, robusteren IS/OOS-Split auf der VOLLEN uebergebenen Historie.
# Beide Mechanismen gleichzeitig zu nutzen (wie hier vorher moeglich) fuehrte
# zu einem doppelten, sich ueberlappenden Ausschluss -- der Optimizer trainierte
# dann nur noch auf ~49% statt ~70% der Historie, und die "Bestaetigung" lief
# auf laengst veralteten statt den echten juengsten Daten. Validiert im
# 28-Kombinationen-Sweep vom 2026-08-26/27: volle Historie rein, optimizer.py
# regelt IS/OOS allein (siehe PIPELINE_UPDATE_AND_28PAIR_SWEEP_2026-08.md).

# --- Startdatum ---
echo ""
echo -e "${BLUE}--- Empfehlung: Rückblick-Zeitraum je Timeframe (Standard bei 'a') ---${NC}"
printf "+------------------+-----------+\n"
printf "| Zeitfenster      | Lookback  |\n"
printf "+------------------+-----------+\n"
printf "| 5m, 15m          |  90 Tage  |\n"
printf "| 30m, 1h          | 548 Tage  |\n"
printf "| 2h               | 730 Tage  |\n"
printf "| 4h, 6h           |1095 Tage  |\n"
printf "| 1d               |1825 Tage  |\n"
printf "+------------------+-----------+\n"
echo "  (IS/OOS-Aufteilung dieser Historie erfolgt weiter unten separat per --is_fraction)"
echo ""
read -p "Startdatum (JJJJ-MM-TT) oder 'a' für Automatik [Standard: a]: " START_DATE_INPUT
START_DATE_INPUT=${START_DATE_INPUT:-a}

DEFAULT_CAPITAL=$("$PYTHON" -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('start_capital',50))" 2>/dev/null || echo "50")
DEFAULT_TRIALS=$("$PYTHON"  -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('num_trials',200))" 2>/dev/null || echo "200")
read -p "Startkapital in USDT [Standard: $DEFAULT_CAPITAL]: " START_CAPITAL; START_CAPITAL=${START_CAPITAL:-$DEFAULT_CAPITAL}
read -p "CPU-Kerne [Standard: -1 für alle]: " N_CORES; N_CORES=${N_CORES:--1}
read -p "Anzahl Trials [Standard: $DEFAULT_TRIALS]: " N_TRIALS; N_TRIALS=${N_TRIALS:-$DEFAULT_TRIALS}

echo ""
echo "Mindest-Trades pro Jahr pro Strategie:"
echo "  Jede Strategie muss diesen Wert pro Jahr erreichen — sonst wird der Trial verworfen."
echo "  Der Wert wird proportional zur Trainingslänge skaliert (Trades/Jahr × Tage / 365)."
echo "  Tipp: 1d-Timeframe = max ~365 Kerzen/Jahr, Envelopes triggern selten."
echo "  Empfehlung: 20 (locker) | 30 (ausgewogen) | 50 (streng)"
read -p "Mindest-Trades/Jahr pro Strategie [Standard: 20]: " MIN_TRADES_PER_YEAR; MIN_TRADES_PER_YEAR=${MIN_TRADES_PER_YEAR:-20}

# --- IS/OOS-Split + K-Fold-Robustheit (2026-08-21 Port von stbot) ---
# Jede Optuna-Studie sieht nur IS_FRACTION der Historie; der Rest dient ausschliesslich
# der Bestaetigung des besten Trials danach (siehe optimizer.py objective()/main()).
# Behebt In-Sample-Overfitting (siehe LIVE_TRADE_FORENSIK_2026-08.md: voller 3-Jahres-
# Backtest sagt "Gate AUS besser", juengste 3 Monate allein sagen "Gate AUS schlechter").
DEFAULT_IS_FRACTION=$("$PYTHON" -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('is_fraction',0.70))" 2>/dev/null || echo "0.70")
DEFAULT_K_FOLDS=$("$PYTHON" -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('k_folds',3))" 2>/dev/null || echo "3")
DEFAULT_MIN_OOS_TRADES=$("$PYTHON" -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('min_oos_trades',10))" 2>/dev/null || echo "10")
echo ""
echo "  IS/OOS-Split: Anteil der Historie, den Optuna beim Optimieren sieht (Rest = Out-of-Sample-Bestaetigung danach)."
read -p "In-Sample-Anteil [Standard: $DEFAULT_IS_FRACTION]: " IS_FRACTION; IS_FRACTION=${IS_FRACTION:-$DEFAULT_IS_FRACTION}
read -p "K-Fold-Teilfenster fuer Robustheits-Score [Standard: $DEFAULT_K_FOLDS]: " K_FOLDS; K_FOLDS=${K_FOLDS:-$DEFAULT_K_FOLDS}
read -p "Mindest-OOS-Trades fuer Bestaetigung [Standard: $DEFAULT_MIN_OOS_TRADES]: " MIN_OOS_TRADES; MIN_OOS_TRADES=${MIN_OOS_TRADES:-$DEFAULT_MIN_OOS_TRADES}

# --- Automatischer Trial-Nachlauf (2026-08-27) ---
# Bei vielen Symbol/Timeframe-Kombinationen reicht ein fester Trial-Wert nicht
# immer aus, um unter den IS/OOS+K-Fold-Constraints ueberhaupt einen gueltigen
# Trial zu finden ("no_valid_trials") -- beim 28-Kombinationen-Lauf am
# 2026-08-26/27 brauchten mehrere Paare (u.a. BTC/2h, ETH/6h, SOL/2h) einen
# Nachlauf mit deutlich mehr Trials. Betrifft v.a. lange, unbeaufsichtigte
# Batch-Laeufe ueber viele Paare -- bei einzelnen manuellen Laeufen kann man
# das genauso gut selbst per Hand mit mehr Trials wiederholen.
echo ""
read -p "Bei 'no_valid_trials' automatisch mit mehr Trials nachlegen? (j/n) [Standard: j]: " AUTO_RETRY_CHOICE
AUTO_RETRY_CHOICE=${AUTO_RETRY_CHOICE:-j}
RETRY_TRIALS=0
if [[ "$AUTO_RETRY_CHOICE" == "j" || "$AUTO_RETRY_CHOICE" == "J" ]]; then
    read -p "Trials fuer den Nachlauf [Standard: 600]: " RETRY_TRIALS; RETRY_TRIALS=${RETRY_TRIALS:-600}
fi

# --- Re-Optimierungs-Sperre umgehen (2026-08-27, Overfeeding-Schutz in optimizer.py) ---
# Standardmaessig ueberspringt optimizer.py bereits bestaetigte/kuerzlich
# gefittete Configs (siehe optimizer.py --recheck-confirmed/--recheck-after-days).
# Fuer eine bewusste komplette Neubewertung (z.B. nach Loeschen aller Configs,
# oder nach einer Methodik-Aenderung wie multi_band_entries/Compounding) muss
# das hier ausdruecklich umgangen werden, sonst werden erst kuerzlich
# bestaetigte Paare blind uebersprungen statt neu geprueft.
echo ""
read -p "Bereits bestaetigte/kuerzlich optimierte Configs TROTZDEM neu pruefen? (j/n) [Standard: n]: " RECHECK_CHOICE
RECHECK_CHOICE=${RECHECK_CHOICE:-n}
RECHECK_ARGS=""
if [[ "$RECHECK_CHOICE" == "j" || "$RECHECK_CHOICE" == "J" ]]; then
    RECHECK_ARGS="--recheck-confirmed --recheck-after-days 0"
fi


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
OVERWRITE_ALL="n"
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

        # Volle Historie -- optimizer.py macht den IS/OOS-Split selbst (--is_fraction)
        if [ "$START_DATE_INPUT" == "a" ]; then
            CURRENT_START_DATE=$(date -d "$lookback_days days ago" +%F)
        else
            CURRENT_START_DATE="$START_DATE_INPUT"
        fi
        CURRENT_END_DATE="$TODAY"

        echo ""
        echo -e "${BLUE}=======================================================${NC}"
        echo -e "${BLUE}  Bearbeite Pipeline für: $symbol ($timeframe)${NC}"
        echo -e "${BLUE}  Zeitraum: $CURRENT_START_DATE  →  $CURRENT_END_DATE  (IS/OOS-Split intern: $IS_FRACTION)${NC}"
        echo -e "${BLUE}=======================================================${NC}"

        # Config-Existenz prüfen (skip/overwrite/all) — Wildcard wie titanbot
        SYM_CLEAN=$(echo "${symbol}" | tr '[:lower:]' '[:upper:]' | tr -d '/: -')
        FOUND_CFG=$(ls src/ltbbot/strategy/configs/config_*${SYM_CLEAN}*_${timeframe}*_envelope.json 2>/dev/null | head -1)
        if [ -n "$FOUND_CFG" ] && [ "$OVERWRITE_ALL" != "j" ]; then
            echo ""
            echo -e "${YELLOW}⚠  Config existiert bereits: $symbol ($timeframe)${NC}"
            read -p "   (ü)berschreiben / (s)kip / (a)lle überschreiben? [s]: " OVERWRITE_CHOICE
            OVERWRITE_CHOICE=${OVERWRITE_CHOICE:-s}
            if [[ "$OVERWRITE_CHOICE" == "s" || "$OVERWRITE_CHOICE" == "S" ]]; then
                echo "  → Übersprungen."; continue
            elif [[ "$OVERWRITE_CHOICE" == "a" || "$OVERWRITE_CHOICE" == "A" ]]; then
                OVERWRITE_ALL="j"
                echo "  → Alle überschreiben."
            else
                echo "  → Wird überschrieben."
            fi
        fi

        run_optimizer_once() {
            local trials="$1"
            local tmp_log
            tmp_log=$(mktemp)
            "$PYTHON" "$OPTIMIZER" \
                --symbols       "$symbol" \
                --timeframes    "$timeframe" \
                --start_date    "$CURRENT_START_DATE" \
                --end_date      "$CURRENT_END_DATE" \
                --jobs          "$N_CORES" \
                --max_drawdown  "$MAX_DD" \
                --start_capital "$START_CAPITAL" \
                --min_win_rate  "$MIN_WR" \
                --trials        "$trials" \
                --min_pnl       "$MIN_PNL" \
                --mode          "$OPTIM_MODE_ARG" \
                --min_trades_per_year "$MIN_TRADES_PER_YEAR" \
                --is_fraction   "$IS_FRACTION" \
                --k_folds       "$K_FOLDS" \
                --min_oos_trades "$MIN_OOS_TRADES" \
                --config_suffix "_envelope" \
                $RECHECK_ARGS 2>&1 | tee "$tmp_log"
            local rc=${PIPESTATUS[0]}
            NO_VALID_TRIALS=0
            if grep -qE "konnte keine g.ltige Konfiguration gefunden werden|no_valid_trials" "$tmp_log"; then
                NO_VALID_TRIALS=1
            fi
            rm -f "$tmp_log"
            return $rc
        }

        echo -e "\n${GREEN}>>> Starte Optimierung für $symbol ($timeframe) ($N_TRIALS Trials)...${NC}"
        run_optimizer_once "$N_TRIALS"
        RC=$?

        if [ "$RETRY_TRIALS" -gt 0 ] && [ "$NO_VALID_TRIALS" -eq 1 ]; then
            echo -e "${YELLOW}⚠  Kein gueltiger Trial bei $N_TRIALS Trials -- Nachlauf mit $RETRY_TRIALS Trials...${NC}"
            run_optimizer_once "$RETRY_TRIALS"
            RC=$?
        fi

        if [ $RC -ne 0 ]; then
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
