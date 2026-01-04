#!/bin/bash
# --- Not-Aus-Schalter ---
# Beendet das Skript sofort, wenn ein Befehl fehlschlägt.
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}======================================================="
echo "      ltbbot Installations-Skript (Envelope Version)"
echo "=======================================================${NC}"

# --- System-Abhängigkeiten installieren ---
echo -e "\n${YELLOW}1/4: Aktualisiere Paketlisten und installiere System-Abhängigkeiten...${NC}"
sudo apt-get update
# Verwende python3-venv statt spezifischer Version für breitere Kompatibilität
sudo apt-get install -y python3 python3-venv git curl jq # jq hinzugefügt (nützlich für shell scripts)
echo -e "${GREEN}✔ System-Abhängigkeiten installiert.${NC}"

# --- Python Virtuelle Umgebung einrichten ---
echo -e "\n${YELLOW}2/4: Erstelle eine isolierte Python-Umgebung (.venv)...${NC}"
# Stelle sicher, dass wir im richtigen Verzeichnis sind (wo install.sh liegt)
INSTALL_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$INSTALL_DIR"

# Entferne alte venv falls sie existiert
if [ -d ".venv" ]; then
    echo -e "${YELLOW}Entferne alte virtuelle Umgebung...${NC}"
    rm -rf .venv
fi

# Erstelle neue venv mit --upgrade-deps für bessere Kompatibilität
python3 -m venv .venv --upgrade-deps
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erstellt.${NC}"

# --- Python-Bibliotheken installieren ---
echo -e "\n${YELLOW}3/4: Aktiviere die virtuelle Umgebung und installiere die notwendigen Python-Bibliotheken...${NC}"
source .venv/bin/activate

# Verwende python3 -m pip für bessere Kompatibilität
python3 -m pip install --upgrade pip setuptools wheel
# Stelle sicher, dass requirements.txt im selben Verzeichnis ist
if [ -f "requirements.txt" ]; then
    python3 -m pip install -r requirements.txt
    echo -e "${GREEN}✔ Alle Python-Bibliotheken wurden erfolgreich installiert.${NC}"
else
    echo -e "${RED}FEHLER: requirements.txt nicht gefunden! Überspringe Python-Bibliotheken.${NC}"
fi
deactivate

# --- Abschluss ---
echo -e "\n${YELLOW}4/4: Setze Ausführungsrechte für alle .sh-Skripte...${NC}"
chmod +x *.sh
# Stelle sicher, dass auch Skripte in Unterverzeichnissen (falls vorhanden) Rechte bekommen
# find . -name "*.sh" -exec chmod +x {} \; # Optional, falls nötig

echo -e "\n${GREEN}======================================================="
echo "✅  Installation erfolgreich abgeschlossen!"
echo ""
echo "Nächste Schritte:"
echo "  1. Erstelle/Bearbeite die 'secret.json' Datei mit deinen API-Keys."
echo "     ( nano secret.json )"
echo "  2. Führe die Optimierungs-Pipeline aus, um Strategie-Konfigs zu erstellen:"
echo "     ( bash ./run_pipeline.sh )"
echo "  3. Konfiguriere 'settings.json', um festzulegen, welche Strategien live laufen sollen."
echo "     ( nano settings.json )"
echo "  4. Starte den Live-Bot mit:"
echo "     ( python3 master_runner.py )"
echo -e "=======================================================${NC}"
