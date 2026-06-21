#!/usr/bin/env bash
# run_analysis.sh — ltbbot Envelope Strategie Analysen
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
RUNNER="$SCRIPT_DIR/src/ltbbot/analysis/analysis_runner.py"
export PYTHONPATH="$SCRIPT_DIR/src:${PYTHONPATH:-}"

# Farben
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'
B='\033[1;34m'; C='\033[0;36m'; W='\033[1;37m'; NC='\033[0m'

NO_TELEGRAM=""
for arg in "$@"; do [[ "$arg" == "--no-telegram" ]] && NO_TELEGRAM="--no-telegram"; done

if [[ ! -f "$PYTHON" ]]; then
    echo -e "${R}Fehler: .venv nicht gefunden. Bitte zuerst erstellen:${NC}"
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

print_menu() {
    clear
    echo -e "${W}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${W}║         ltbbot — Envelope Strategie Analysen                ║${NC}"
    echo -e "${W}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${C}  Walk-Forward / Robustheit${NC}"
    echo -e "  ${Y}1${NC}  Walk-Forward Lookback-Analyse"
    echo -e "  ${Y}2${NC}  Envelope Parameter Walk-Forward (SL / Period)"
    echo ""
    echo -e "${C}  Risiko / Kosten${NC}"
    echo -e "  ${Y}3${NC}  Slippage & Fee Impact"
    echo -e "  ${Y}4${NC}  Monte Carlo Simulation"
    echo ""
    echo -e "${C}  Portfolio / Pair-Auswahl${NC}"
    echo -e "  ${Y}5${NC}  Anti-Korrelations-Portfolio"
    echo -e "  ${Y}6${NC}  Kelly Position Sizing"
    echo ""
    echo -e "${C}  Strategie-Einblicke${NC}"
    echo -e "  ${Y}7${NC}  Regime Performance Analyse"
    echo -e "  ${Y}8${NC}  Tageszeit-Analyse"
    echo -e "  ${Y}9${NC}  Drawdown Duration Analyse"
    echo ""
    echo -e "  ${G}0${NC}  Alle Analysen nacheinander (Batch)"
    echo ""
    echo -e "${W}──────────────────────────────────────────────────────────────${NC}"
}

ask() {
    local prompt="$1" default="$2" var_name="$3"
    read -rp "  $prompt [$default]: " val
    val="${val:-$default}"
    printf -v "$var_name" "%s" "$val"
}

run_mode() {
    local mode="$1" cap="$2" lookback="$3" extra="${4:-}"
    "$PYTHON" "$RUNNER" \
        --mode "$mode" \
        --capital "$cap" \
        --lookback "$lookback" \
        $NO_TELEGRAM \
        $extra
}

# ── Hauptschleife ────────────────────────────────────────────────────────────
while true; do
    print_menu
    read -rp "  Analyse wählen (0-9, q=Beenden): " choice

    case "$choice" in
    q|Q) echo -e "\n${G}Tschüss!${NC}"; exit 0 ;;

    1)
        echo -e "\n${B}Walk-Forward Lookback-Analyse${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Min. Trades pro Fenster" "5"  MINT
        run_mode 1 "$CAP" "auto" "--min-trades $MINT"
        ;;

    2)
        echo -e "\n${B}Envelope Parameter Walk-Forward${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        run_mode 2 "$CAP" "auto"
        ;;

    3)
        echo -e "\n${B}Slippage & Fee Impact${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        run_mode 3 "$CAP" "$LB"
        ;;

    4)
        echo -e "\n${B}Monte Carlo Simulation${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        ask "Simulationen"         "10000" SIMS
        run_mode 4 "$CAP" "$LB" "--simulations $SIMS"
        ;;

    5)
        echo -e "\n${B}Anti-Korrelations-Portfolio${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        run_mode 5 "$CAP" "$LB"
        ;;

    6)
        echo -e "\n${B}Kelly Position Sizing${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        run_mode 6 "$CAP" "$LB"
        ;;

    7)
        echo -e "\n${B}Regime Performance Analyse${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        run_mode 7 "$CAP" "$LB"
        ;;

    8)
        echo -e "\n${B}Tageszeit-Analyse${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        run_mode 8 "$CAP" "$LB"
        ;;

    9)
        echo -e "\n${B}Drawdown Duration Analyse${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        run_mode 9 "$CAP" "$LB"
        ;;

    0)
        echo -e "\n${B}Batch-Modus: Alle Analysen${NC}"
        ask "Startkapital (USDT)"  "50"  CAP
        ask "Lookback Tage"        "365" LB
        for m in 1 2 3 4 5 6 7 8 9; do
            echo -e "\n${Y}─── Analyse $m ───${NC}"
            run_mode "$m" "$CAP" "$LB" || true
        done
        ;;

    *)
        echo -e "${R}Ungültige Auswahl.${NC}"
        sleep 1
        ;;
    esac

    echo ""
    read -rp "  Enter für Menü..." _
done
