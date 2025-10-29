#!/bin/bash
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}======================================================="
echo "      ltbbot Vollautomatische Optimierungs-Pipeline (Envelope)"
echo -e "=======================================================${NC}"

# --- Pfade definieren ---
VENV_PATH=".venv/bin/activate"
OPTIMIZER="src/ltbbot/analysis/optimizer.py" # Nur noch der Optimizer wird gebraucht

# --- Umgebung aktivieren ---
source "$VENV_PATH"
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert.${NC}"

# --- AUFRÄUM-ASSISTENT ---
echo -e "\n${YELLOW}Möchtest du alle alten, generierten Konfigurationen vor dem Start löschen?${NC}"
read -p "Dies wird für einen kompletten Neustart empfohlen. (j/n) [Standard: n]: " CLEANUP_CHOICE; CLEANUP_CHOICE=${CLEANUP_CHOICE:-n}
if [[ "$CLEANUP_CHOICE" == "j" || "$CLEANUP_CHOICE" == "J" ]]; then
    # Lösche nur Envelope Configs (oder alle?)
    echo -e "${YELLOW}Lösche alte Konfigurationen (config_*_envelope.json)...${NC}"; rm -f src/ltbbot/strategy/configs/config_*_envelope.json; echo -e "${GREEN}✔ Aufräumen abgeschlossen.${NC}"
else
    echo -e "${GREEN}✔ Alte Konfigurationen werden beibehalten.${NC}"
fi

# --- Interaktive Abfrage ---
read -p "Handelspaar(e) eingeben (ohne /USDT, z.B. BTC ETH): " SYMBOLS
read -p "Zeitfenster eingeben (z.B. 1h 4h): " TIMEFRAMES
echo -e "\n${BLUE}--- Empfehlung: Optimaler Rückblick-Zeitraum ---${NC}"
printf "+-------------+--------------------------------+\n"; printf "| Zeitfenster | Empfohlener Rückblick (Tage)   |\n"; printf "+-------------+--------------------------------+\n"; printf "| 5m, 15m     | 30 - 90 Tage                   |\n"; printf "| 30m, 1h     | 180 - 365 Tage                 |\n"; printf "| 2h, 4h      | 365 - 730 Tage                 |\n"; printf "| 6h, 1d      | 730 - 1825 Tage                |\n"; printf "+-------------+--------------------------------+\n"
# Automatik für Startdatum basierend auf Zeitfenster (vereinfacht)
read -p "Startdatum (JJJJ-MM-TT) oder 'a' für Automatik [Standard: a]: " START_DATE_INPUT; START_DATE_INPUT=${START_DATE_INPUT:-a}
read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_DATE; END_DATE=${END_DATE:-$(date +%F)}
read -p "Startkapital in USDT [Standard: 1000]: " START_CAPITAL; START_CAPITAL=${START_CAPITAL:-1000}
read -p "CPU-Kerne für Optimierung [Standard: -1 für alle]: " N_CORES; N_CORES=${N_CORES:--1}
read -p "Anzahl Optimierungs-Trials [Standard: 200]: " N_TRIALS; N_TRIALS=${N_TRIALS:-200}

echo -e "\n${YELLOW}Wähle einen Optimierungs-Modus:${NC}"; echo "  1) Strenger Modus (Profit mit Constraints)"; echo "  2) 'Finde das Beste' (Max Score, nur DD Constraint)"
read -p "Auswahl (1-2) [Standard: 1]: " OPTIM_MODE_CHOICE; OPTIM_MODE_CHOICE=${OPTIM_MODE_CHOICE:-1}
if [ "$OPTIM_MODE_CHOICE" == "1" ]; then
    OPTIM_MODE_ARG="strict"
    read -p "Max Drawdown % [Standard: 30]: " MAX_DD; MAX_DD=${MAX_DD:-30}
    # WinRate ist für Envelope weniger kritisch, Standard 0
    read -p "Min Win-Rate % [Standard: 0 (Ignorieren)]: " MIN_WR; MIN_WR=${MIN_WR:-0}
    read -p "Min PnL % [Standard: 0]: " MIN_PNL; MIN_PNL=${MIN_PNL:-0}
else
    OPTIM_MODE_ARG="best_profit"
    # Evtl. höheres DD erlauben im Best Profit Modus
    read -p "Max Drawdown % [Standard: 50]: " MAX_DD; MAX_DD=${MAX_DD:-50}
    MIN_WR=0 # Keine Win-Rate-Beschränkung
    MIN_PNL=-99999 # Negativer PnL erlaubt
fi

# Schleife für Symbole und Zeitrahmen
for symbol in $SYMBOLS; do
    for timeframe in $TIMEFRAMES; do
        echo -e "\n${BLUE}=======================================================${NC}"
        echo -e "${BLUE}  Bearbeite Pipeline für: $symbol ($timeframe)${NC}"

        # Automatisches Startdatum berechnen
        if [ "$START_DATE_INPUT" == "a" ]; then
             lookback_days=365 # Standard
             # Lookback basierend auf Zeitfenster anpassen
             case "$timeframe" in
                 5m|15m) lookback_days=60 ;;
                 30m|1h) lookback_days=180 ;;
                 2h|4h) lookback_days=365 ;;
                 6h|1d) lookback_days=730 ;;
             esac
             CURRENT_START_DATE=$(date -d "$lookback_days days ago" +%F)
             CURRENT_END_DATE="$END_DATE" # Enddatum bleibt wie eingegeben
             echo -e "${BLUE}  Automatischer Lookback: $lookback_days Tage${NC}"
        else
            CURRENT_START_DATE="$START_DATE_INPUT"
            CURRENT_END_DATE="$END_DATE"
        fi
        echo -e "${BLUE}  Datenzeitraum: $CURRENT_START_DATE bis $CURRENT_END_DATE${NC}"
        echo -e "${BLUE}=======================================================${NC}"

        # --- Nur noch Stufe: Optimierung ---
        echo -e "\n${GREEN}>>> Starte Optimierung für $symbol ($timeframe)...${NC}"
        # Führe den Optimizer aus
        python3 "$OPTIMIZER" \
            --symbols "$symbol" \
            --timeframes "$timeframe" \
            --start_date "$CURRENT_START_DATE" \
            --end_date "$CURRENT_END_DATE" \
            --jobs "$N_CORES" \
            --max_drawdown "$MAX_DD" \
            --start_capital "$START_CAPITAL" \
            --min_win_rate "$MIN_WR" \
            --trials "$N_TRIALS" \
            --min_pnl "$MIN_PNL" \
            --mode "$OPTIM_MODE_ARG" \
            --config_suffix "_envelope" # Wichtig: Suffix für Config-Dateien

        # Fehlerprüfung
        if [ $? -ne 0 ]; then
            echo -e "${RED}Fehler im Optimierer für $symbol ($timeframe). Überspringe...${NC}"
        else
            echo -e "${GREEN}✔ Optimierung für $symbol ($timeframe) abgeschlossen.${NC}"
        fi
    done
done

deactivate
echo -e "\n${BLUE}✔ Alle Pipeline-Aufgaben abgeschlossen!${NC}"
