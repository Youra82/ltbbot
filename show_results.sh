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
    echo -e "\n${BLUE}Generiere interaktive Charts...${NC}"
    python3 src/ltbbot/analysis/interactive_status.py
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ Charts wurden generiert!${NC}"
    else
        echo -e "${RED}❌ Fehler beim Generieren der Charts.${NC}"
    fi
    
    deactivate
    exit 0
fi

deactivate
