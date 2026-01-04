#!/bin/bash
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Beende das Skript sofort bei Fehlern
set -e

# Stelle sicher, dass wir im richtigen Verzeichnis sind
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$SCRIPT_DIR"

VENV_PATH=".venv/bin/activate"
RESULTS_SCRIPT="src/ltbbot/analysis/show_results.py"

# Überprüfe, ob die virtuelle Umgebung existiert
if [ ! -f "$VENV_PATH" ]; then
    echo -e "${RED}❌ Fehler: Virtuelle Umgebung nicht gefunden!${NC}"
    echo -e "${YELLOW}Installiere LTBBot...${NC}"
    ./install.sh
    if [ ! -f "$VENV_PATH" ]; then
        echo -e "${RED}❌ Installation fehlgeschlagen!${NC}"
        exit 1
    fi
fi

# Aktiviere die virtuelle Umgebung
source "$VENV_PATH"

# Überprüfe, ob alle erforderlichen Pakete installiert sind
echo -e "${YELLOW}Überprüfe Python-Abhängigkeiten...${NC}"
python3 -m pip install --quiet --upgrade pip setuptools wheel 2>/dev/null || true

# Versuche, requirements.txt zu installieren wenn noch nicht done
if ! python3 -c "import pandas, plotly" 2>/dev/null; then
    echo -e "${YELLOW}Installiere fehlende Pakete...${NC}"
    python3 -m pip install --quiet -r requirements.txt
fi

# --- ERWEITERTES MODUS-MENÜ ---
echo -e "\n${YELLOW}Wähle einen Analyse-Modus für ltbbot:${NC}"
echo "  1) Einzel-Analyse (jede Strategie wird isoliert getestet)"
echo "  2) Manuelle Portfolio-Simulation (du wählst das Team)"
echo "  3) Automatische Portfolio-Optimierung (der Bot wählt das beste Team)"
echo "  4) Interaktive Charts (Entry/Exit-Signale nur, keine Indikatoren)"
read -p "Auswahl (1-4) [Standard: 1]: " MODE
MODE=${MODE:-1}

# Rufe das (angepasste) Python-Skript auf
python3 "$RESULTS_SCRIPT" --mode "$MODE"

# Deaktiviere die virtuelle Umgebung
deactivate
