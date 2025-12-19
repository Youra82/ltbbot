#!/bin/bash

# Farben für eine schönere Ausgabe definieren
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Hauptverzeichnis des Projekts bestimmen
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$PROJECT_ROOT" # Stelle sicher, dass wir im Projektverzeichnis sind

# Funktion, um den Inhalt einer Datei formatiert auszugeben
show_file_content() {
    local FILE_PATH_REL=$1 # Relativer Pfad zur Datei
    local FILE_PATH_ABS="$PROJECT_ROOT/$FILE_PATH_REL"

    # Bestimme eine beschreibende Überschrift basierend auf dem Dateinamen/Pfad
    local DESCRIPTION=$(basename "$FILE_PATH_REL")

    if [ -f "${FILE_PATH_ABS}" ]; then
        echo -e "\n${BLUE}======================================================================${NC}"
        echo -e "${YELLOW}DATEI: ${DESCRIPTION}${NC}"
        echo -e "${CYAN}Pfad: ${FILE_PATH_ABS}${NC}" # Zeige absoluten Pfad
        echo -e "${BLUE}----------------------------------------------------------------------${NC}"

        # Spezielle Zensur-Logik nur für secret.json
        if [[ "$DESCRIPTION" == "secret.json" ]]; then
            echo -e "${YELLOW}HINWEIS: Sensible Daten in secret.json wurden zensiert.${NC}"
            # Verwende jq für robustere Zensur (falls installiert)
            if command -v jq &> /dev/null; then
                 jq '
                 (.ltbbot[]? | select(.apiKey) | .apiKey) |= "[ZENSIERT]" |
                 (.ltbbot[]? | select(.secret) | .secret) |= "[ZENSIERT]" |
                 (.ltbbot[]? | select(.password) | .password) |= "[ZENSIERT]" |
                 (select(.telegram) | .telegram.bot_token?) |= "[ZENSIERT]" |
                 (select(.telegram) | .telegram.chat_id?) |= "[ZENSIERT]"
                 ' "${FILE_PATH_ABS}" | cat -n
            else
                 # Fallback mit sed (weniger robust bei komplexem JSON)
                 sed -E 's/("apiKey"|"secret"|"password"|"bot_token"|"chat_id"): ".*"/"\1": "[ZENSIERT]"/g' "${FILE_PATH_ABS}" | cat -n
            fi
        else
            # Zeige Inhalt mit Zeilennummern
            cat -n "${FILE_PATH_ABS}"
        fi

        echo -e "${BLUE}======================================================================${NC}"
    else
        echo -e "\n${RED}WARNUNG: Datei nicht gefunden unter ${FILE_PATH_ABS}${NC}"
    fi
}

# --- ANZEIGE ALLER RELEVANTEN CODE-DATEIEN ---
echo -e "${BLUE}======================================================================${NC}"
echo "              Vollständige Code-Dokumentation des ltbbot" # Titel angepasst
echo -e "${BLUE}======================================================================${NC}"

# Finde alle relevanten Dateien (angepasst für ltbbot Struktur).
# Schließe .venv, .git, __pycache__ und secret.json vorerst aus.
# Füge tracker Verzeichnis hinzu (optional anzeigen?)
mapfile -t FILE_LIST < <(find . -path './.venv' -prune -o \
                               -path './.git' -prune -o \
                               -path './secret.json' -prune -o \
                               -path '*/__pycache__' -prune -o \
                               -path './data/cache' -prune -o \
                               -path './artifacts/db' -prune -o \
                               -path './artifacts/results' -prune -o \
                               -path './artifacts/tracker' -prune -o \
                               -path './logs' -prune -o \
                               \( -name "*.py" -o -name "*.sh" -o -name "*.json" -o -name "*.txt" -o -name ".gitignore" -o -name "*.md" -o -name "LICENSE" \) -print | sed 's|^\./||' | sort
                        )

# Zeige zuerst alle anderen Dateien an
for filepath in "${FILE_LIST[@]}"; do
    # Stelle sicher, dass die Datei existiert (find kann manchmal seltsame Ergebnisse liefern)
    if [ -f "$filepath" ]; then
        show_file_content "$filepath"
    fi
done

# Zeige die secret.json als LETZTE Datei an (zensiert)
if [ -f "secret.json" ]; then
    show_file_content "secret.json"
fi

# Zeige optional Inhalte von Tracker-Dateien (erste paar Zeilen?)
# echo -e "\n${YELLOW}Tracker-Dateien (Auszug):${NC}"
# find ./artifacts/tracker -name "*.json" -print -exec head -n 10 {} \; -exec echo "..." \;

# --- ANZEIGE DER PROJEKTSTRUKTUR AM ENDE ---
echo -e "\n\n${BLUE}======================================================="
echo "             Aktuelle Projektstruktur (ltbbot)" # Titel angepasst
echo -e "=======================================================${NC}"

# Eine Funktion, die eine Baumstruktur mit Standard-Tools emuliert
# Pfade angepasst (keine models, dafür tracker)
list_structure() {
    find . -path './.venv' -prune -o \
           -path './.git' -prune -o \
           -path '*/__pycache__' -prune -o \
           -path './data/cache' -prune -o \
           -path './artifacts/db' -prune -o \
           -path './artifacts/results' -prune -o \
           -path './artifacts/tracker' -prune -o \
           -path './logs' -prune -o \
           -maxdepth 4 -print | sed -e 's;[^/]*/;|____;g;s;____|; |;g' | grep -v -e '|____.git' # .git nochmal explizit filtern
}

list_structure

echo -e "${BLUE}=======================================================${NC}"
