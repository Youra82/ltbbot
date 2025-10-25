# src/ltbbot/analysis/optimizer.py
import os
import sys
import json
import optuna
import numpy as np
import argparse
import logging
import warnings
from joblib import Parallel, delayed # Für Parallelisierung

# Logging und Warnungen konfigurieren (wie im Original)
# os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2' # Nicht mehr nötig
logging.getLogger('optuna').setLevel(logging.WARNING)
# logging.getLogger('tensorflow').setLevel(logging.ERROR) # Nicht mehr nötig
# logging.getLogger('absl').setLevel(logging.ERROR) # Nicht mehr nötig
# warnings.filterwarnings('ignore', category=UserWarning, module='keras') # Nicht mehr nötig

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# Verwende den Backtester für Envelope
from ltbbot.analysis.backtester import load_data, run_envelope_backtest # Umbenannt!
# from ltbbot.utils.telegram import send_message # Behalten für Benachrichtigungen
from ltbbot.analysis.evaluator import evaluate_dataset # Behalten für Datenqualität

optuna.logging.set_verbosity(optuna.logging.WARNING)

# Globale Variablen für die Objective-Funktion
HISTORICAL_DATA = None
CURRENT_SYMBOL = None
CURRENT_TIMEFRAME = None
CONFIG_SUFFIX = "" # Für Varianten wie _macd (hier nicht relevant, aber Struktur beibehalten)

# Constraints und Einstellungen (werden durch Argumente überschrieben)
MAX_DRAWDOWN_CONSTRAINT = 0.30
MIN_WIN_RATE_CONSTRAINT = 0.0 # Standardmäßig keine Win-Rate-Beschränkung
MIN_PNL_CONSTRAINT = 0.0
START_CAPITAL = 1000
OPTIM_MODE = "strict" # Oder "best_profit"

def create_safe_filename(symbol, timeframe):
    """Erstellt einen sicheren Dateinamen aus Symbol und Zeitrahmen."""
    return f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"

def objective(trial):
    """Optuna Objective-Funktion zur Optimierung der Envelope-Parameter."""
    global HISTORICAL_DATA, START_CAPITAL, CURRENT_TIMEFRAME, OPTIM_MODE, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT

    # --- Parameter für die Envelope-Strategie vorschlagen ---
    avg_type = trial.suggest_categorical('average_type', ['SMA', 'EMA', 'WMA', 'DCM'])
    avg_period = trial.suggest_int('average_period', 5, 50) # Beispielbereich

    # Envelopes optimieren (Beispiel für 3 Bänder mit steigendem Abstand)
    env1 = trial.suggest_float('env1', 0.005, 0.05) # 0.5% bis 5%
    env2 = trial.suggest_float('env2', env1 + 0.005, 0.10) # Mindestens 0.5% größer als env1, bis 10%
    env3 = trial.suggest_float('env3', env2 + 0.005, 0.15) # Mindestens 0.5% größer als env2, bis 15%
    envelopes = sorted([env1, env2, env3]) # Sicherstellen, dass sie sortiert sind

    # Trigger Delta als Prozentsatz
    trigger_delta_pct = trial.suggest_float('trigger_price_delta_pct', 0.01, 0.2) # 0.01% bis 0.2%

    # --- Risikoparameter vorschlagen ---
    leverage = trial.suggest_int('leverage', 1, 25) # Hebel
    # balance_fraction_pct = trial.suggest_int('balance_fraction_pct', 50, 100) # Fester Wert? Oder optimieren?
    balance_fraction_pct = 100 # Fester Wert für Einfachheit
    stop_loss_pct = trial.suggest_float('stop_loss_pct', 0.1, 2.0) # 0.1% bis 2.0% SL

    # --- Parameter-Dict für den Backtester zusammenstellen ---
    params = {
        'strategy': {
            'average_type': avg_type,
            'average_period': avg_period,
            'envelopes': envelopes,
            'trigger_price_delta_pct': round(trigger_delta_pct, 4)
        },
        'risk': {
            'margin_mode': 'isolated', # Fest?
            'balance_fraction_pct': balance_fraction_pct,
            'leverage': leverage,
            'stop_loss_pct': round(stop_loss_pct, 2)
        },
         'behavior': { # Wichtig für Backtester
            'use_longs': True,
            'use_shorts': True
         }
        # 'market' wird nicht benötigt, da Daten direkt übergeben werden
    }

    # --- Backtest ausführen ---
    # Stelle sicher, dass HISTORICAL_DATA und START_CAPITAL korrekt gesetzt sind
    if HISTORICAL_DATA is None or START_CAPITAL <= 0:
        raise ValueError("HISTORICAL_DATA oder START_CAPITAL nicht korrekt initialisiert.")

    result = run_envelope_backtest(HISTORICAL_DATA.copy(), params, START_CAPITAL)

    # --- Ergebnisse extrahieren und bewerten ---
    pnl = result.get('total_pnl_pct', -1000.0)
    drawdown = result.get('max_drawdown_pct', 100.0) / 100.0 # Umwandlung in Dezimal für Vergleich
    trades = result.get('trades_count', 0)
    win_rate = result.get('win_rate', 0.0)

    # --- Constraints prüfen (Pruning) ---
    if OPTIM_MODE == "strict":
        if drawdown > MAX_DRAWDOWN_CONSTRAINT or win_rate < MIN_WIN_RATE_CONSTRAINT or pnl < MIN_PNL_CONSTRAINT or trades < 20: # Mindestanzahl Trades
             raise optuna.exceptions.TrialPruned()
    elif OPTIM_MODE == "best_profit":
        # Im "best_profit" Modus nur auf Drawdown und Mindest-Trades prüfen
        if drawdown > MAX_DRAWDOWN_CONSTRAINT or trades < 20:
             raise optuna.exceptions.TrialPruned()

    # --- Zielwert zurückgeben (z.B. PnL oder Sharpe Ratio) ---
    # Hier verwenden wir PnL, aber eine risikoadjustierte Metrik wäre besser (z.B. Calmar Ratio)
    score = pnl / (drawdown * 100) if drawdown > 0.01 else pnl # Calmar Ratio (vereinfacht)
    # return pnl
    return score


# --- Main Funktion (angepasst von JaegerBot) ---
def main():
    global HISTORICAL_DATA, CURRENT_SYMBOL, CURRENT_TIMEFRAME, CONFIG_SUFFIX, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, START_CAPITAL, OPTIM_MODE

    parser = argparse.ArgumentParser(description="Parameter-Optimierung für ltbbot (Envelope-Strategie)")
    parser.add_argument('--symbols', required=True, type=str, help="Symbole, getrennt durch Leerzeichen (z.B. BTC ETH)")
    parser.add_argument('--timeframes', required=True, type=str, help="Zeitrahmen, getrennt durch Leerzeichen (z.B. 1h 4h)")
    parser.add_argument('--start_date', required=True, type=str, help="Startdatum (JJJJ-MM-TT)")
    parser.add_argument('--end_date', required=True, type=str, help="Enddatum (JJJJ-MM-TT)")
    parser.add_argument('--jobs', required=True, type=int, help="Anzahl paralleler Jobs (-1 für alle CPU-Kerne)")
    parser.add_argument('--max_drawdown', required=True, type=float, help="Maximal erlaubter Drawdown in % (z.B. 30)")
    parser.add_argument('--start_capital', required=True, type=float, help="Startkapital für Backtests (z.B. 1000)")
    parser.add_argument('--min_win_rate', required=True, type=float, help="Minimale Win-Rate in % (z.B. 55)")
    parser.add_argument('--trials', required=True, type=int, help="Anzahl der Optuna Trials (z.B. 200)")
    parser.add_argument('--min_pnl', required=True, type=float, help="Minimaler Gesamt-PnL in % (z.B. 0)")
    parser.add_argument('--mode', required=True, type=str, choices=['strict', 'best_profit'], help="Optimierungsmodus")
    # parser.add_argument('--threshold', type=float, help="Nicht verwendet für Envelope") # Entfernt
    # parser.add_argument('--use_macd_filter', type=str, help="Nicht verwendet für Envelope") # Entfernt
    parser.add_argument('--config_suffix', type=str, default="_envelope", help="Suffix für Config-Dateinamen")
    # parser.add_argument('--top_n', type=int, default=0) # Argument aus run_pipeline_automated.sh - hier nicht direkt verwendet
    args = parser.parse_args()

    # Globale Variablen setzen
    CONFIG_SUFFIX = args.config_suffix
    MAX_DRAWDOWN_CONSTRAINT = args.max_drawdown / 100.0
    MIN_WIN_RATE_CONSTRAINT = args.min_win_rate
    MIN_PNL_CONSTRAINT = args.min_pnl
    START_CAPITAL = args.start_capital
    OPTIM_MODE = args.mode
    N_TRIALS = args.trials

    symbols, timeframes = args.symbols.split(), args.timeframes.split()
    TASKS = [{'symbol': f"{s}/USDT:USDT", 'timeframe': tf} for s in symbols for tf in timeframes]

    optuna_results = [] # Sammelt die besten Ergebnisse für jede Task

    # --- Schleife durch alle Symbol/Zeitrahmen-Kombinationen ---
    for task in TASKS:
        symbol, timeframe = task['symbol'], task['timeframe']
        CURRENT_SYMBOL = symbol
        CURRENT_TIMEFRAME = timeframe

        print(f"\n===== Optimiere: {symbol} ({timeframe}) {CONFIG_SUFFIX} =====")

        # --- Daten laden ---
        HISTORICAL_DATA = load_data(symbol, timeframe, args.start_date, args.end_date)
        if HISTORICAL_DATA is None or HISTORICAL_DATA.empty:
            logger.warning(f"Keine Daten für {symbol} ({timeframe}) geladen. Überspringe.")
            continue

        # --- Datenqualität bewerten (optional aber empfohlen) ---
        print("\n--- Bewertung der Datensatz-Qualität ---")
        evaluation = evaluate_dataset(HISTORICAL_DATA.copy(), timeframe) # evaluator.py muss ggf. angepasst werden
        print(f"Note: {evaluation['score']} / 10\n" + "\n".join(evaluation['justification']) + "\n----------------------------------------")
        if evaluation['score'] < 4: # Beispiel: Mindestscore für Optimierung
            logger.warning(f"Datensatzqualität zu gering ({evaluation['score']}/10). Überspringe Optimierung.")
            # continue # Oder trotzdem optimieren?

        # --- Optuna Studie erstellen/laden ---
        DB_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'optuna_studies_ltbbot.db') # Eigene DB für ltbbot
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        STORAGE_URL = f"sqlite:///{DB_FILE}?timeout=60" # Timeout gegen "database locked"

        safe_filename = create_safe_filename(symbol, timeframe)
        study_name = f"env_{safe_filename}_{OPTIM_MODE}" # Eindeutiger Studienname

        try:
             study = optuna.create_study(storage=STORAGE_URL, study_name=study_name, direction="maximize", load_if_exists=True)

             # Vorhandene Trials prüfen und ggf. löschen, wenn Parameter sich geändert haben (optional)
             # Man könnte hier prüfen, ob die Suchräume in study.best_params zu den aktuellen passen

             # --- Optimierung starten ---
             n_jobs = args.jobs
             if n_jobs == 1:
                 # Kein Parallelismus, direkte Ausführung
                 study.optimize(objective, n_trials=N_TRIALS, show_progress_bar=True)
             else:
                 # Parallelisierung mit joblib (Standard-Optuna-Verhalten)
                 study.optimize(objective, n_trials=N_TRIALS, n_jobs=n_jobs, show_progress_bar=True)

             # --- Bestes Ergebnis extrahieren ---
             valid_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
             if not valid_trials:
                 print(f"\n❌ FEHLER: Für {symbol} ({timeframe}) konnte keine gültige Konfiguration gefunden werden (alle Trials pruned?).")
                 continue

             best_trial = study.best_trial # Optuna findet das beste basierend auf dem Return-Wert von objective
             best_params_optuna = best_trial.params
             best_score = best_trial.value # Der zurückgegebene Score (z.B. Calmar Ratio)

             # Führe den Backtest mit den besten Parametern erneut aus, um alle Metriken zu bekommen
             final_params_dict = {
                 'strategy': {
                     'average_type': best_params_optuna['average_type'],
                     'average_period': best_params_optuna['average_period'],
                     'envelopes': sorted([best_params_optuna['env1'], best_params_optuna['env2'], best_params_optuna['env3']]),
                     'trigger_price_delta_pct': round(best_params_optuna['trigger_price_delta_pct'], 4)
                 },
                 'risk': {
                     'margin_mode': 'isolated',
                     'balance_fraction_pct': 100, # Annahme fester Wert
                     'leverage': best_params_optuna['leverage'],
                     'stop_loss_pct': round(best_params_optuna['stop_loss_pct'], 2)
                 },
                 'behavior': {'use_longs': True, 'use_shorts': True}
             }
             final_result = run_envelope_backtest(HISTORICAL_DATA.copy(), final_params_dict, START_CAPITAL)

             print("\n--- Bestes Ergebnis ---")
             print(f"  Score (Calmar o.ä.): {best_score:.4f}")
             print(f"  PnL: {final_result.get('total_pnl_pct'):.2f}%")
             print(f"  Max Drawdown: {final_result.get('max_drawdown_pct'):.2f}%")
             print(f"  Trades: {final_result.get('trades_count')}")
             print(f"  Win Rate: {final_result.get('win_rate'):.2f}%")
             print(f"  Beste Parameter: {best_params_optuna}")

             optuna_results.append({
                  'symbol': symbol,
                  'timeframe': timeframe,
                  'score': best_score,
                  'pnl_pct': final_result.get('total_pnl_pct'),
                  'max_drawdown_pct': final_result.get('max_drawdown_pct'),
                  'win_rate': final_result.get('win_rate'),
                  'trades': final_result.get('trades_count'),
                  'params': best_params_optuna,
                  'config_dict': final_params_dict # Speichere das vollständige Dict
             })


             # --- Konfigurationsdatei speichern ---
             config_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
             os.makedirs(config_dir, exist_ok=True)
             config_filename = f'config_{safe_filename}{CONFIG_SUFFIX}.json'
             config_output_path = os.path.join(config_dir, config_filename)

             # Erstelle das finale JSON-Objekt
             config_output = {
                 "market": {"symbol": symbol, "timeframe": timeframe},
                 "strategy": final_params_dict['strategy'],
                 "risk": final_params_dict['risk'],
                 "behavior": final_params_dict['behavior']
             }

             with open(config_output_path, 'w') as f:
                 json.dump(config_output, f, indent=4)
             print(f"\n✔ Beste Konfiguration wurde in '{config_output_path}' gespeichert.")

        except Exception as e:
             logger.error(f"Schwerwiegender Fehler während der Optimierung für {symbol} ({timeframe}): {e}", exc_info=True)
             # Hier könnte man den Loop abbrechen oder weitermachen

    # --- Nach allen Tasks: Zusammenfassung (optional) ---
    if optuna_results:
         print("\n===== Optimierungs-Zusammenfassung =====")
         sorted_results = sorted(optuna_results, key=lambda x: x['score'], reverse=True)
         for res in sorted_results:
              print(f"- {res['symbol']} ({res['timeframe']}): Score={res['score']:.2f}, PnL={res['pnl_pct']:.2f}%, DD={res['max_drawdown_pct']:.2f}%, WR={res['win_rate']:.2f}%, Trades={res['trades']}")
         # Hier könnte man die besten N Strategien auswählen und speichern, wie in run_pipeline_automated.sh angedeutet

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    main()
