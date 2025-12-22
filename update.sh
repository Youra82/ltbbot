#!/bin/bash
# Robustes und sicheres Update-Skript für ltbbot
# Beendet sich nicht sofort bei Fehlern, behandelt diese stattdessen
set +e  # Deaktiviere automatischen Abbruch bei Fehler

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}--- Sicheres Update für ltbbot wird ausgeführt ---${NC}"

# Bestimme das Projektverzeichnis dynamisch
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$PROJECT_ROOT" || { echo -e "${RED}Fehler: Kann nicht ins Projektverzeichnis wechseln${NC}"; exit 1; }

# 1. Sichere NUR secret.json (settings.json soll vom Repo aktualisiert werden!)
echo -e "${YELLOW}1/5: Erstelle Backup von 'secret.json'...${NC}"
if [ -f "secret.json" ]; then
    cp secret.json secret.json.bak
    echo -e "${GREEN}✔ Backup von secret.json erstellt.${NC}"
else
    echo -e "${YELLOW}⚠ Keine secret.json gefunden (wird beim ersten Start erstellt).${NC}"
fi

# 2. Hole die neuesten Daten von GitHub
echo -e "${YELLOW}2/5: Hole den neuesten Stand von GitHub (origin/main)...${NC}"
if git fetch origin main; then
    echo -e "${GREEN}✔ Fetch abgeschlossen.${NC}"
else
    echo -e "${RED}✘ Fehler beim Abrufen von GitHub. Überprüfe deine Internetverbindung.${NC}"
    exit 1
fi

# 3. Setze das lokale Verzeichnis hart auf den Stand von GitHub zurück
echo -e "${YELLOW}3/5: Setze alle Dateien auf den neuesten Stand zurück (inkl. settings.json!)...${NC}"
if git reset --hard origin/main; then
    echo -e "${GREEN}✔ Reset auf origin/main durchgeführt - settings.json wurde aktualisiert!${NC}"
else
    echo -e "${RED}✘ Fehler beim Reset. Update abgebrochen.${NC}"
    exit 1
fi

# 4. Stelle NUR secret.json aus dem Backup wieder her
echo -e "${YELLOW}4/5: Stelle 'secret.json' aus Backup wieder her...${NC}"
if [ -f "secret.json.bak" ]; then
    cp secret.json.bak secret.json
    rm secret.json.bak
    echo -e "${GREEN}✔ secret.json wiederhergestellt.${NC}"
else
    echo -e "${YELLOW}⚠ Kein Backup gefunden (normal beim ersten Update).${NC}"
fi

# 5. Aktualisiere Python-Abhängigkeiten (nur wenn nötig)
echo -e "${YELLOW}5/5: Prüfe und aktualisiere Python-Bibliotheken...${NC}"
VENV_PATH="$PROJECT_ROOT/.venv/bin/activate"

# Funktion zum Neuerstellen des venv
rebuild_venv() {
    echo -e "${YELLOW}🔄 Erstelle virtuelle Umgebung neu...${NC}"
    rm -rf .venv
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip --no-cache-dir -q
    python -m pip install -r requirements.txt --no-cache-dir -q
    deactivate
}

if [ -f "$VENV_PATH" ]; then
    source "$VENV_PATH"
    
    # Teste pip und installiere Pakete
    echo -e "${BLUE}Teste pip und installiere Pakete...${NC}"
    
    # Versuche pip-Update (zeige Fehler wenn vorhanden)
    python -m pip install --upgrade pip --no-cache-dir -q 2>&1 | grep -v "^$" || true
    PIP_UPDATE_EXIT=${PIPESTATUS[0]}
    
    # Versuche requirements Installation (zeige Fehler wenn vorhanden)
    python -m pip install -r requirements.txt --no-cache-dir -q 2>&1 | grep -v "^$" || true
    PIP_INSTALL_EXIT=${PIPESTATUS[0]}
    
    # Wenn einer der beiden Befehle fehlgeschlagen ist
    if [ $PIP_UPDATE_EXIT -ne 0 ] || [ $PIP_INSTALL_EXIT -ne 0 ]; then
        deactivate
        echo -e "${RED}⚠ Pip-Installation fehlgeschlagen (Exit: $PIP_UPDATE_EXIT, $PIP_INSTALL_EXIT). Erstelle virtuelle Umgebung neu...${NC}"
        rebuild_venv
        echo -e "${GREEN}✔ Virtuelle Umgebung neu erstellt und Pakete installiert.${NC}"
    else
        echo -e "${GREEN}✔ Python-Bibliotheken sind aktuell.${NC}"
        deactivate
    fi
else
    echo -e "${YELLOW}⚠ Virtuelle Umgebung nicht gefunden!${NC}"
    rebuild_venv
    echo -e "${GREEN}✔ Virtuelle Umgebung neu erstellt.${NC}"
fi

# 6. Setze die Ausführungsrechte für alle Skripte erneut
echo -e "${YELLOW}6/6: Setze Ausführungsrechte für alle .sh-Skripte...${NC}"
chmod +x *.sh 2>/dev/null
echo -e "${GREEN}✔ Ausführungsrechte gesetzt.${NC}"

echo -e "\n${GREEN}======================================================="
echo "✅ Update erfolgreich abgeschlossen!"
echo "   - Code und settings.json wurden von GitHub aktualisiert"
echo "   - secret.json blieb unverändert"
echo "   - Bitte starte master_runner.py neu, falls er lief"
echo -e "=======================================================${NC}"
