#!/bin/bash
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Stelle sicher, dass wir im richtigen Verzeichnis sind
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$SCRIPT_DIR"

VENV_PATH=".venv/bin/activate"
VENV_PYTHON=".venv/bin/python"
VENV_PIP=".venv/bin/pip"
RESULTS_SCRIPT="src/ltbbot/analysis/show_results.py"

# Überprüfe, ob die virtuelle Umgebung vollständig ist
if [ ! -f "$VENV_PYTHON" ] || [ ! -f "$VENV_PIP" ]; then
    echo -e "${YELLOW}⚠️  Virtuelle Umgebung nicht vollständig - wird neu erstellt...${NC}"
    rm -rf .venv 2>/dev/null || true
    python3 -m venv .venv --upgrade-deps
    echo -e "${GREEN}✔ Neue virtuelle Umgebung erstellt.${NC}"
fi

# Aktiviere die virtuelle Umgebung
source "$VENV_PATH"

# Upgrade pip, setuptools, wheel
echo -e "${YELLOW}Überprüfe Python-Abhängigkeiten...${NC}"
"$VENV_PIP" install --upgrade pip setuptools wheel --quiet

# Versuche, requirements.txt zu installieren wenn noch nicht vorhanden
if ! "$VENV_PYTHON" -c "import pandas, plotly" 2>/dev/null; then
    echo -e "${YELLOW}Installiere fehlende Pakete...${NC}"
    if ! "$VENV_PIP" install -r requirements.txt --quiet 2>/dev/null; then
        echo -e "${YELLOW}Versuche mit --break-system-packages (PEP 668 Kompatibilität)...${NC}"
        "$VENV_PIP" install --break-system-packages -r requirements.txt --quiet
    fi
    echo -e "${GREEN}✔ Pakete installiert.${NC}"
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
"$VENV_PYTHON" "$RESULTS_SCRIPT" --mode "$MODE"

# Deaktiviere die virtuelle Umgebung
deactivate

# Rufe das (angepasste) Python-Skript auf
python3 "$RESULTS_SCRIPT" --mode "$MODE"

# Deaktiviere die virtuelle Umgebung
deactivate
