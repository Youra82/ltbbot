#!/bin/bash
set -e

echo "--- Sicheres Update wird ausgeführt (Robuste Version) ---"

# 1. Sichere lokale Dateien die nicht von Git verwaltet werden sollen
echo "1. Erstelle ein Backup von 'secret.json', Configs und lokalen settings.json-Overrides..."
cp secret.json secret.json.bak

# Sichere die komplette Config-Verzeichnis (Envelope-Parameter, vom VPS-eigenen
# Optuna-Optimizer geschrieben) -- git reset --hard wuerde sie sonst auf den
# letzten GEPUSHTEN (ggf. wochenalten) Stand zuruecksetzen, VPS-seitige
# Re-Optimierungen werden nie nach GitHub gepusht (siehe
# infra_mbot_settings_json_git_drift, gleiches Muster hier bestaetigt
# 2026-09-24: ein update.sh-Lauf hat das aktive Portfolio unbemerkt auf ein
# altes, DOGE/AVAX enthaltendes settings.json zurueckgesetzt).
if [ -d "src/ltbbot/strategy/configs" ]; then
    cp -r src/ltbbot/strategy/configs src_ltbbot_strategy_configs.bak
fi

# Sichere Analyse-Ergebnisse UND das aktive Portfolio aus settings.json (werden
# durch git reset ueberschrieben). WICHTIG (2026-08-27, erweitert 2026-09-24):
# diese Werte werden bewusst aus dem AKTUELLEN LOKALEN Stand gesichert und nach
# dem Reset wieder reingeschrieben -- ein Wert, der im Repo gepusht wird, hat
# auf diesem VPS also KEINE Wirkung, solange hier schon ein (ggf. veralteter)
# lokaler Wert steht. Soll ein neuer Wert dauerhaft gelten, muss er einmalig
# HIER manuell in settings.json gesetzt werden, nicht nur im Repo.
# active_strategies ist der wichtigste dieser Werte: das ist das ECHTE, vom
# Auto-Portfolio-Optimizer laufend aktualisierte Live-Portfolio -- ohne diese
# Sicherung wuerde JEDER update.sh-Lauf den Bot auf ein potenziell wochenaltes
# Portfolio zuruecksetzen (live beobachtet 2026-09-24: DOGE/AVAX-Trades nach
# einem Deploy, obwohl beide laengst aus dem echten Portfolio entfernt waren).
SAVED_LB=""
SAVED_OOS=""
SAVED_STRATEGIES=""
SAVED_USE_AUTO_OPT=""
if [ -f settings.json ]; then
    SAVED_LB=$(python3 -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('backtest_lookback_weeks',''))" 2>/dev/null || true)
    SAVED_OOS=$(python3 -c "import json; s=json.load(open('settings.json')); print(s.get('optimization_settings',{}).get('oos_reference_date','') or '')" 2>/dev/null || true)
    SAVED_STRATEGIES=$(python3 -c "import json; s=json.load(open('settings.json')); print(json.dumps(s.get('live_trading_settings',{}).get('active_strategies',[])))" 2>/dev/null || true)
    SAVED_USE_AUTO_OPT=$(python3 -c "import json; s=json.load(open('settings.json')); print(json.dumps(s.get('live_trading_settings',{}).get('use_auto_optimizer_results')))" 2>/dev/null || true)
fi

# 2. Hole die neuesten Daten von GitHub
echo "2. Hole den neuesten Stand von GitHub..."
git fetch origin

# 3. Setze das lokale Verzeichnis hart auf den Stand von GitHub zurück
echo "3. Setze alle Dateien auf den neuesten Stand zurück und verwerfe lokale Änderungen..."
git reset --hard origin/main

# 4. Stelle die API-Schlüssel aus dem Backup wieder her
echo "4. Stelle den Inhalt von 'secret.json' aus dem Backup wieder her..."
cp secret.json.bak secret.json
rm secret.json.bak

# Einmalige Release-Uebernahme (2026-10-01): Normalerweise gewinnen die VPS-eigenen
# Configs und active_strategies (woechentliche Automatik, siehe unten). Liegt im Repo
# eine NEUE Release-Kennung (deploy/release.json), die dieser VPS noch nicht
# uebernommen hat, gilt EINMALIG der Repo-Stand: Config-Verzeichnis wird komplett
# durch das Repo ersetzt, active_strategies kommt aus dem Repo. Danach wird die
# Kennung in .applied_release vermerkt (untracked, ueberlebt git reset) -- alle
# spaeteren update.sh-Laeufe verhalten sich wieder wie gewohnt.
APPLY_RELEASE=false
RELEASE_ID=""
if [ -f deploy/release.json ]; then
    RELEASE_ID=$(python3 -c "import json; print(json.load(open('deploy/release.json')).get('release_id',''))" 2>/dev/null || true)
    APPLIED_ID=$(cat .applied_release 2>/dev/null || true)
    if [ -n "$RELEASE_ID" ] && [ "$RELEASE_ID" != "$APPLIED_ID" ]; then
        APPLY_RELEASE=true
        echo "   🚀 Neues Release '$RELEASE_ID' gefunden -- Configs und aktives Portfolio werden EINMALIG aus dem Repo übernommen."
    fi
fi

# Stelle die Config-Verzeichnis aus dem Backup wieder her (lokale, VPS-eigene
# Optuna-Ergebnisse gewinnen ueber den git-Stand -- siehe Kommentar oben bei
# der Sicherung). Neue, nur in git existierende Config-Dateien bleiben dabei
# erhalten, da hier nur ueberschrieben, nie geloescht wird.
if [ "$APPLY_RELEASE" = true ]; then
    rm -rf src_ltbbot_strategy_configs.bak
    # Nicht im Repo enthaltene (alte VPS-)Configs entfernen -- git reset loescht keine untracked Dateien
    git clean -fq -- src/ltbbot/strategy/configs/
    echo "   ✅ Config-Verzeichnis aus Release '$RELEASE_ID' übernommen ($(ls src/ltbbot/strategy/configs/config_*_envelope.json 2>/dev/null | wc -l) Configs)."
elif [ -d "src_ltbbot_strategy_configs.bak" ]; then
    cp -r src_ltbbot_strategy_configs.bak/. src/ltbbot/strategy/configs/
    rm -rf src_ltbbot_strategy_configs.bak
    echo "   ✅ Config-Verzeichnis (VPS-eigene Optuna-Ergebnisse) wiederhergestellt."
fi

# Stelle Analyse-Ergebnisse UND das aktive Portfolio in settings.json wieder
# her (falls vorhanden). SAVED_STRATEGIES/SAVED_USE_AUTO_OPT laufen ueber
# Umgebungsvariablen statt direkter Shell-Interpolation in den Python-Code --
# das JSON kann Anfuehrungszeichen enthalten, die eine '$VAR'-Einbettung
# zerbrechen wuerden.
if [ "$APPLY_RELEASE" != true ] && [ -n "$SAVED_LB" ] && [ "$SAVED_LB" != "" ]; then
    python3 -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['backtest_lookback_weeks'] = int('$SAVED_LB')
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null && echo "   ✅ backtest_lookback_weeks=$SAVED_LB wiederhergestellt." || true
fi
if [ "$APPLY_RELEASE" != true ] && [ -n "$SAVED_OOS" ] && [ "$SAVED_OOS" != "None" ]; then
    python3 -c "
import json
s = json.load(open('settings.json'))
s.setdefault('optimization_settings', {})['oos_reference_date'] = '$SAVED_OOS'
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null && echo "   ✅ oos_reference_date=$SAVED_OOS wiederhergestellt." || true
fi
if [ "$APPLY_RELEASE" = true ]; then
    echo "   ✅ active_strategies aus Release '$RELEASE_ID' übernommen ($(python3 -c "import json; print(len(json.load(open('settings.json'))['live_trading_settings']['active_strategies']))" 2>/dev/null || echo '?') Strategien)."
elif [ -n "$SAVED_STRATEGIES" ] && [ "$SAVED_STRATEGIES" != "[]" ] && [ "$SAVED_STRATEGIES" != "" ]; then
    SAVED_STRATEGIES="$SAVED_STRATEGIES" python3 -c "
import json, os
strategies = json.loads(os.environ['SAVED_STRATEGIES'])
s = json.load(open('settings.json'))
s.setdefault('live_trading_settings', {})['active_strategies'] = strategies
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null && echo "   ✅ active_strategies ($(echo "$SAVED_STRATEGIES" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo '?') Strategien) wiederhergestellt." || true
fi
if [ -n "$SAVED_USE_AUTO_OPT" ] && [ "$SAVED_USE_AUTO_OPT" != "null" ] && [ "$SAVED_USE_AUTO_OPT" != "" ]; then
    SAVED_USE_AUTO_OPT="$SAVED_USE_AUTO_OPT" python3 -c "
import json, os
val = json.loads(os.environ['SAVED_USE_AUTO_OPT'])
s = json.load(open('settings.json'))
s.setdefault('live_trading_settings', {})['use_auto_optimizer_results'] = val
json.dump(s, open('settings.json', 'w'), indent=4)
" 2>/dev/null && echo "   ✅ use_auto_optimizer_results=$SAVED_USE_AUTO_OPT wiederhergestellt." || true
fi

# Release als uebernommen vermerken, sobald Configs + Portfolio gesetzt sind (vor den
# venv-Schritten, damit ein spaeterer Fehler das Release nicht erneut ausloest und
# dabei Ergebnisse der woechentlichen Automatik ueberschreibt).
if [ "$APPLY_RELEASE" = true ]; then
    echo "$RELEASE_ID" > .applied_release
    echo "   ✅ Release '$RELEASE_ID' als übernommen vermerkt (.applied_release)."
fi

# 5. Lösche den Python-Cache, um alte Code-Versionen zu entfernen
echo "5. Lösche alten Python-Cache für einen sauberen Neustart..."
# .venv auslassen; rm -rf statt -delete und || true: laeuft parallel ein Cron-Zyklus, legt Python
# neue .pyc an ("Directory not empty") -- das darf wegen set -e das Update nicht abbrechen.
find . -path ./.venv -prune -o -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# 6. Setze die Ausführungsrechte für alle Skripte
echo "6. Setze Ausführungsrechte für alle .sh-Skripte..."
chmod +x *.sh

# 7. venv-Gesundheitscheck — Rebuild falls pip kaputt ist
echo "7. Prüfe venv-Gesundheit..."
VENV_OK=true
if [ ! -f ".venv/bin/python3" ]; then
    echo "   venv fehlt — wird neu erstellt..."
    VENV_OK=false
elif ! .venv/bin/python3 -c "import pip" 2>/dev/null; then
    echo "   pip nicht importierbar — venv wird neu erstellt..."
    VENV_OK=false
elif ! .venv/bin/pip --version 2>/dev/null | grep -q pip; then
    echo "   pip defekt — venv wird neu erstellt..."
    VENV_OK=false
elif ! .venv/bin/python3 -c "import ccxt, pandas, ta" 2>/dev/null; then
    # Frueher wurde hier pip._vendor.resolvelib.structs.RequirementInformation geprueft --
    # das gibt es in neueren pip-Versionen nicht mehr, die Pruefung schlug IMMER fehl und
    # die venv wurde bei jedem Update neu gebaut (2026-10-02). Jetzt: laufen die Pakete?
    echo "   Pakete fehlen/defekt — venv wird neu erstellt..."
    VENV_OK=false
fi

if [ "$VENV_OK" = false ]; then
    rm -rf .venv
    python3 -m venv .venv
    echo "   Installiere Dependencies..."
    .venv/bin/pip install --quiet -r requirements.txt
    echo "   ✅ venv neu erstellt und Dependencies installiert."
else
    echo "   ✅ venv ist gesund."
fi

echo "✅ Update erfolgreich abgeschlossen. Dein Bot ist jetzt auf dem neuesten Stand."
