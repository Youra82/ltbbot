#!/bin/bash
# Sofortiger Abbruch bei Fehlern
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}--- Sicheres Update für ltbbot wird ausgeführt ---${NC}"

# Bestimme das Projektverzeichnis dynamisch
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$PROJECT_ROOT" # Wechsle ins Projektverzeichnis

# 1. Sichere die einzigen Dateien, die lokal wichtig sind
echo -e "${YELLOW}1/6: Erstelle Backups von 'secret.json' und 'settings.json'...${NC}"
cp secret.json secret.json.bak
cp settings.json settings.json.bak
echo -e "${GREEN}✔ Backups erstellt.${NC}"

# 2. Hole die neuesten Daten von GitHub
echo -e "${YELLOW}2/6: Hole den neuesten Stand von GitHub (origin/main)...${NC}"
git fetch origin main # Spezifiziere den Branch
echo -e "${GREEN}✔ Fetch abgeschlossen.${NC}"

# 3. Setze das lokale Verzeichnis hart auf den Stand von GitHub zurück
echo -e "${YELLOW}3/6: Setze alle Dateien auf den neuesten Stand zurück (verwirft lokale Code-Änderungen!)...${NC}"
git reset --hard origin/main
echo -e "${GREEN}✔ Reset auf origin/main durchgeführt.${NC}"

# 4. Stelle die Konfigurationen aus dem Backup wieder her
echo -e "${YELLOW}4/6: Stelle 'secret.json' und 'settings.json' aus Backups wieder her...${NC}"
cp secret.json.bak secret.json
cp settings.json.bak settings.json
rm secret.json.bak
rm settings.json.bak
echo -e "${GREEN}✔ Konfigurationen wiederhergestellt.${NC}"

# 5. Aktualisiere Python-Abhängigkeiten (falls requirements.txt geändert wurde)
echo -e "${YELLOW}5/6: Aktualisiere Python-Bibliotheken gemäß requirements.txt...${NC}"
# Prüfe ob venv existiert
VENV_PATH="$PROJECT_ROOT/.venv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    source "$VENV_PATH"
    pip install --upgrade pip
    pip install -r requirements.txt
    deactivate
    echo -e "${GREEN}✔ Python-Bibliotheken aktualisiert.${NC}"
else
    echo -e "${YELLOW}WARNUNG: Virtuelle Umgebung nicht gefunden. Überspringe Pip-Update. Führe ggf. install.sh aus.${NC}"
fi

# 6. Setze die Ausführungsrechte für alle Skripte erneut
echo -e "${YELLOW}6/6: Setze Ausführungsrechte für alle .sh-Skripte...${NC}"
chmod +x *.sh
# Optional: Rechte für Skripte in Unterordnern
# find . -name "*.sh" -exec chmod +x {} \;
echo -e "${GREEN}✔ Ausführungsrechte gesetzt.${NC}"


echo -e "\n${GREEN}======================================================="
echo "✅ Update erfolgreich abgeschlossen. Dein ltbbot ist jetzt auf dem neuesten Stand."
echo "   Bitte starte den master_runner.py neu, falls er lief."
echo -e "=======================================================${NC}"
