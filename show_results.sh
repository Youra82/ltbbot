#!/bin/bash
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'
VENV_PATH=".venv/bin/activate"
RESULTS_SCRIPT="src/ltbbot/analysis/show_results.py" # Pfad angepasst

source "$VENV_PATH"

# --- ERWEITERTES MODUS-MENÜ ---
echo -e "\n${YELLOW}Wähle einen Analyse-Modus für ltbbot:${NC}"
echo "  1) Einzel-Analyse (jede Strategie wird isoliert getestet)"
echo "  2) Manuelle Portfolio-Simulation (du wählst das Team)"
echo "  3) Automatische Portfolio-Optimierung (der Bot wählt das beste Team)"
echo "  4) Interaktive Charts (mit Envelopes und SMAs)"
read -p "Auswahl (1-4) [Standard: 1]: " MODE
MODE=${MODE:-1}

# Rufe das (angepasste) Python-Skript auf
python3 "$RESULTS_SCRIPT" --mode "$MODE"

# --- OPTION 4: INTERAKTIVE CHARTS ---
if [ "$MODE" == "4" ]; then
    echo -e "\n${YELLOW}========== INTERAKTIVE CHARTS ===========${NC}"
    echo ""
    read -p "Symbol (z.B. DOGE/USDT): " SYMBOL
    read -p "Timeframe (z.B. 4h, 1h) [Standard: 4h]: " TIMEFRAME
    TIMEFRAME=${TIMEFRAME:-4h}
    read -p "Start-Kapital [Standard: 1000]: " START_CAPITAL
    START_CAPITAL=${START_CAPITAL:-1000}
    read -p "Letzte N Tage anzeigen (oder leer für alle): " WINDOW
    read -p "Telegram versenden? (j/n) [Standard: n]: " SEND_TELEGRAM
    
    TELEGRAM_FLAG=""
    if [[ "$SEND_TELEGRAM" =~ ^[jJyY]$ ]]; then
        TELEGRAM_FLAG="--send-telegram"
    fi
    
    WINDOW_FLAG=""
    if [ ! -z "$WINDOW" ]; then
        WINDOW_FLAG="--window $WINDOW"
    fi
    
    echo -e "\n${BLUE}Generiere Chart...${NC}"
    python3 src/ltbbot/analysis/interactive_status.py \
        --symbol "$SYMBOL" \
        --timeframe "$TIMEFRAME" \
        --start-capital "$START_CAPITAL" \
        $WINDOW_FLAG \
        $TELEGRAM_FLAG
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ Chart wurde generiert!${NC}"
    else
        echo -e "${RED}❌ Fehler beim Generieren des Charts.${NC}"
    fi
    
    deactivate
    exit 0
fi

deactivate
