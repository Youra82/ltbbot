#!/bin/bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}=== ltbbot Update ===${NC}"

# 1. Backup secret.json
if [ -f "secret.json" ]; then
    cp secret.json secret.json.bak
fi

# 2. Git update
echo "Hole Updates von GitHub..."
git fetch origin main
git reset --hard origin/main

# 3. Restore secret.json
if [ -f "secret.json.bak" ]; then
    mv secret.json.bak secret.json
fi

# 4. Update pip packages (optional)
if [ -f ".venv/bin/activate" ]; then
    echo "Aktualisiere Python-Pakete..."
    source .venv/bin/activate
    pip install -r requirements.txt -q
    deactivate
else
    echo -e "${YELLOW}Hinweis: Keine .venv gefunden. Führe './install.sh' aus.${NC}"
fi

# 5. Set permissions
chmod +x *.sh 2>/dev/null || true

echo -e "${GREEN}✅ Update abgeschlossen! Bitte master_runner.py neu starten.${NC}"
