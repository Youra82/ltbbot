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

# 1. Sichere die einzigen Dateien, die lokal wichtig sind
echo -e "${YELLOW}1/6: Erstelle Backups von 'secret.json' und 'settings.json'...${NC}"
if [ -f "secret.json" ]; then
    cp secret.json secret.json.bak
fi
if [ -f "settings.json" ]; then
    cp settings.json settings.json.bak
fi
echo -e "${GREEN}✔ Backups erstellt.${NC}"

# 2. Hole die neuesten Daten von GitHub
echo -e "${YELLOW}2/6: Hole den neuesten Stand von GitHub (origin/main)...${NC}"
if git fetch origin main; then
    echo -e "${GREEN}✔ Fetch abgeschlossen.${NC}"
else
    echo -e "${RED}✘ Fehler beim Abrufen von GitHub. Überprüfe deine Internetverbindung.${NC}"
    exit 1
fi

# 3. Setze das lokale Verzeichnis hart auf den Stand von GitHub zurück
echo -e "${YELLOW}3/6: Setze alle Dateien auf den neuesten Stand zurück (verwirft lokale Code-Änderungen!)...${NC}"
if git reset --hard origin/main; then
    echo -e "${GREEN}✔ Reset auf origin/main durchgeführt.${NC}"
else
    echo -e "${RED}✘ Fehler beim Reset. Update abgebrochen.${NC}"
    exit 1
fi

# 4. Stelle die Konfigurationen aus dem Backup wieder her
echo -e "${YELLOW}4/6: Stelle 'secret.json' und 'settings.json' aus Backups wieder her...${NC}"
if [ -f "secret.json.bak" ]; then
    cp secret.json.bak secret.json
    rm secret.json.bak
fi
if [ -f "settings.json.bak" ]; then
    cp settings.json.bak settings.json
    rm settings.json.bak
fi
echo -e "${GREEN}✔ Konfigurationen wiederhergestellt.${NC}"

# 5. Aktualisiere Python-Abhängigkeiten (falls requirements.txt geändert wurde)
echo -e "${YELLOW}5/6: Aktualisiere Python-Bibliotheken gemäß requirements.txt...${NC}"
VENV_PATH="$PROJECT_ROOT/.venv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    source "$VENV_PATH"
    
    # Verwende python -m pip für robusteres Update
    echo -e "${BLUE}Aktualisiere pip...${NC}"
    if ! python -m pip install --upgrade pip --no-cache-dir 2>/dev/null; then
        echo -e "${YELLOW}⚠ Pip-Update fehlgeschlagen. Versuche Reparatur...${NC}"
        
        # Versuche pip zu reparieren
        if python -m ensurepip --upgrade 2>/dev/null; then
            echo -e "${BLUE}Pip wurde repariert. Versuche erneut...${NC}"
            python -m pip install --upgrade --force-reinstall pip --no-cache-dir
        else
            echo -e "${RED}⚠ Pip-Reparatur fehlgeschlagen. Installiere Pakete mit altem pip...${NC}"
        fi
    fi
    
    # Installiere requirements
    echo -e "${BLUE}Installiere Python-Pakete...${NC}"
    if python -m pip install -r requirements.txt --no-cache-dir; then
        echo -e "${GREEN}✔ Python-Bibliotheken aktualisiert.${NC}"
    else
        echo -e "${RED}⚠ Warnung: Einige Pakete konnten nicht installiert werden.${NC}"
        echo -e "${YELLOW}Versuche es manuell mit: source .venv/bin/activate && pip install -r requirements.txt${NC}"
    fi
    
    deactivate
else
    echo -e "${RED}⚠ WARNUNG: Virtuelle Umgebung nicht gefunden!${NC}"
    echo -e "${YELLOW}Führe './install.sh' aus, um die Umgebung neu zu erstellen.${NC}"
fi

# 6. Setze die Ausführungsrechte für alle Skripte erneut
echo -e "${YELLOW}6/6: Setze Ausführungsrechte für alle .sh-Skripte...${NC}"
chmod +x *.sh 2>/dev/null
echo -e "${GREEN}✔ Ausführungsrechte gesetzt.${NC}"

echo -e "\n${GREEN}======================================================="
echo "✅ Update erfolgreich abgeschlossen. Dein ltbbot ist jetzt auf dem neuesten Stand."
echo "   Bitte starte den master_runner.py neu, falls er lief."
echo -e "=======================================================${NC}"
