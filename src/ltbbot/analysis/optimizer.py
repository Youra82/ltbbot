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
# KEINE Tqdm Imports

# Logging konfigurieren
# Optuna Logs auf WARNING reduzieren, um Balken nicht zu stören
logging.getLogger('optuna').setLevel(logging.WARNING)
optuna.logging.set_verbosity(optuna.logging.WARNING)

# Standard-Logger für dieses Skript
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# Verwende den Backtester für Envelope
from ltbbot.analysis.backtester import load_data, run_envelope_backtest
from ltbbot.analysis.evaluator import evaluate_dataset

# Globale Variablen für die Objective-Funktion
HISTORICAL_DATA = None
CURRENT_SYMBOL = None
CURRENT_TIMEFRAME = None
CONFIG_SUFFIX = ""

# Constraints und Einstellungen
MAX_DRAWDOWN_CONSTRAINT = 0.30
MIN_WIN_RATE_CONSTRAINT = 0.0
MIN_PNL_CONSTRAINT = 0.0
START_CAPITAL = 1000
OPTIM_MODE = "strict"
MIN_TRADES_FOR_VALID = 20

def create_safe_filename(symbol, timeframe):
    """Erstellt einen sicheren Dateinamen aus Symbol und Zeitrahmen."""
    return f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"

def objective(trial):
    """Optuna Objective-Funktion zur Optimierung der Envelope-Parameter."""
    global HISTORICAL_DATA, START_CAPITAL, CURRENT_TIMEFRAME, OPTIM_MODE, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, MIN_TRADES_FOR_VALID

    # --- Parameter vorschlagen ---
    avg_type = trial.suggest_categorical('average_type', ['SMA', 'EMA', 'WMA', 'DCM'])
    avg_period = trial.suggest_int('average_period', 5, 50)
    env1 = trial.suggest_float('env1', 0.005, 0.05)
    env2 = trial.suggest_float('env2', env1 + 0.005, 0.10)
    env3 = trial.suggest_float('env3', env2 + 0.005, 0.15)
    envelopes = sorted([env1, env2, env3])
    trigger_delta_pct = trial.suggest_float('trigger_price_delta_pct', 0.01, 0.2)
    leverage = trial.suggest_int('leverage', 1, 15)
    risk_per_entry_pct = trial.suggest_float('risk_per_entry_pct', 0.1, 1.0)
    stop_loss_pct = trial.suggest_float('stop_loss_pct', 0.5, 5.0)

    # --- Parameter-Dict ---
    params = {
        'strategy': {
            'average_type': avg_type, 'average_period': avg_period, 'envelopes': envelopes,
            'trigger_price_delta_pct': round(trigger_delta_pct, 4)
        },
        'risk': {
            'margin_mode': 'isolated', 'risk_per_entry_pct': round(risk_per_entry_pct, 2),
            'leverage': leverage, 'stop_loss_pct': round(stop_loss_pct, 2)
        },
        'behavior': {'use_longs': True, 'use_shorts': True}
    }

    # --- Backtest ---
    if HISTORICAL_DATA is None or START_CAPITAL <= 0:
        # Verwende logger statt print im Objective
        # logger.error("...") # Wird durch backtester_logger unterdrückt, wenn auf ERROR gesetzt
        raise ValueError("HISTORICAL_DATA oder START_CAPITAL nicht korrekt initialisiert.")

    result = run_envelope_backtest(HISTORICAL_DATA.copy(), params, START_CAPITAL)

    # --- Ergebnisse ---
    pnl = result.get('total_pnl_pct', -1000.0)
    drawdown_pct_for_pruning = result.get('max_drawdown_pct', 100.0)
    drawdown_decimal_for_pruning = drawdown_pct_for_pruning / 100.0
    trades = result.get('trades_count', 0)
    win_rate = result.get('win_rate', 0.0)

    # --- Pruning ---
    prune = False
    if OPTIM_MODE == "strict":
        if drawdown_decimal_for_pruning > MAX_DRAWDOWN_CONSTRAINT or win_rate < MIN_WIN_RATE_CONSTRAINT or pnl < MIN_PNL_CONSTRAINT or trades < MIN_TRADES_FOR_VALID:
            prune = True
    elif OPTIM_MODE == "best_profit":
        if drawdown_decimal_for_pruning > MAX_DRAWDOWN_CONSTRAINT or trades < MIN_TRADES_FOR_VALID:
            prune = True

    if prune:
        raise optuna.exceptions.TrialPruned()

    # --- Zielwert (Score = PnL) ---
    score = pnl
    return score


# --- Main Funktion ---
def main():
    global HISTORICAL_DATA, CURRENT_SYMBOL, CURRENT_TIMEFRAME, CONFIG_SUFFIX, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, START_CAPITAL, OPTIM_MODE, MIN_TRADES_FOR_VALID

    parser = argparse.ArgumentParser(description="Parameter-Optimierung für ltbbot (Envelope-Strategie)")
    parser.add_argument('--symbols', required=True, type=str)
    # ... (alle anderen Argumente wie gehabt) ...
    parser.add_argument('--timeframes', required=True, type=str)
    parser.add_argument('--start_date', required=True, type=str)
    parser.add_argument('--end_date', required=True, type=str)
    parser.add_argument('--jobs', required=True, type=int)
    parser.add_argument('--max_drawdown', required=True, type=float)
    parser.add_argument('--start_capital', required=True, type=float)
    parser.add_argument('--min_win_rate', required=True, type=float)
    parser.add_argument('--trials', required=True, type=int)
    parser.add_argument('--min_pnl', required=True, type=float)
    parser.add_argument('--mode', required=True, type=str, choices=['strict', 'best_profit'])
    parser.add_argument('--config_suffix', type=str, default="_envelope")
    parser.add_argument('--min_trades', type=int, default=20)
    args = parser.parse_args()

    # Globale Variablen setzen
    CONFIG_SUFFIX = args.config_suffix
    MAX_DRAWDOWN_CONSTRAINT = args.max_drawdown / 100.0
    MIN_WIN_RATE_CONSTRAINT = args.min_win_rate
    MIN_PNL_CONSTRAINT = args.min_pnl
    START_CAPITAL = args.start_capital
    OPTIM_MODE = args.mode
    N_TRIALS = args.trials
    MIN_TRADES_FOR_VALID = args.min_trades

    symbols, timeframes = args.symbols.split(), args.timeframes.split()
    TASKS = [{'symbol': f"{s.upper()}/USDT:USDT", 'timeframe': tf} for s in symbols for tf in timeframes]

    optuna_results = []

    for task in TASKS:
        symbol, timeframe = task['symbol'], task['timeframe']
        CURRENT_SYMBOL = symbol
        CURRENT_TIMEFRAME = timeframe

        logger.info(f"\n===== Optimiere: {symbol} ({timeframe}) {CONFIG_SUFFIX} =====")

        # --- Daten laden ---
        try:
            HISTORICAL_DATA = load_data(symbol, timeframe, args.start_date, args.end_date)
            if HISTORICAL_DATA is None or HISTORICAL_DATA.empty:
                logger.warning(f"Keine Daten für {symbol} ({timeframe}) geladen. Überspringe.")
                continue
        except Exception as e:
            logger.error(f"Fehler beim Laden der Daten für {symbol} ({timeframe}): {e}", exc_info=True)
            continue

        # --- Datenqualität bewerten ---
        try:
            logger.info("\n--- Bewertung der Datensatz-Qualität ---")
            evaluation = evaluate_dataset(HISTORICAL_DATA.copy(), timeframe)
            logger.info(f"Note: {evaluation['score']} / 10\n" + "\n".join(evaluation['justification']) + "\n----------------------------------------")
            if evaluation['score'] < 4:
                logger.warning(f"Datensatzqualität möglicherweise gering ({evaluation['score']}/10).")
        except Exception as e:
            logger.warning(f"Fehler bei der Datensatzbewertung: {e}.")


        # --- Optuna Studie ---
        DB_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'optuna_studies_ltbbot.db')
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        STORAGE_URL = f"sqlite:///{DB_FILE}?timeout=60"
        safe_filename = create_safe_filename(symbol, timeframe)
        study_name = f"{safe_filename}{CONFIG_SUFFIX}_{OPTIM_MODE}"

        # Vor dem Optimize-Aufruf Logger holen und Level merken/setzen
        backtester_logger = logging.getLogger('ltbbot.analysis.backtester')
        original_level = backtester_logger.level
        backtester_logger.setLevel(logging.ERROR) # Warnings unterdrücken

        try:
            study = optuna.create_study(storage=STORAGE_URL, study_name=study_name, direction="maximize", load_if_exists=True)

            n_jobs = args.jobs
            logger.info(f"Starte Optuna-Optimierung mit {N_TRIALS} Trials und {n_jobs} Job(s)... (Mit Standard show_progress_bar)")

            # *** ZURÜCK ZUM STANDARD-AUFRUF (wie bei JaegerBot/TitanBot) ***
            study.optimize(
                objective,
                n_trials=N_TRIALS,
                n_jobs=n_jobs,
                show_progress_bar=True # Standard-Balken aktivieren
                # Keine callbacks-Liste
            )
            # ***************************************************************

        except Exception as e:
            logger.error(f"Schwerwiegender Fehler während der Optuna-Studie für {symbol} ({timeframe}): {e}", exc_info=True)
            # Level trotzdem zurücksetzen
            backtester_logger.setLevel(original_level)
            continue # Nächsten Task versuchen
        finally:
             # Stelle sicher, dass Level immer zurückgesetzt wird
             backtester_logger.setLevel(original_level)
             logger.info("Backtester-Logging-Level wiederhergestellt.")


        # --- Bestes Ergebnis ---
        valid_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if not valid_trials:
            logger.error(f"\n❌ FEHLER: Für {symbol} ({timeframe}) konnte keine gültige Konfiguration gefunden werden.")
            continue

        best_trial = study.best_trial
        best_params_optuna = best_trial.params
        best_score = best_trial.value # PnL

        # Finaler Backtest mit besten Parametern
        final_params_dict = {
            'strategy': {
                'average_type': best_params_optuna['average_type'], 'average_period': best_params_optuna['average_period'],
                'envelopes': sorted([best_params_optuna['env1'], best_params_optuna['env2'], best_params_optuna['env3']]),
                'trigger_price_delta_pct': round(best_params_optuna['trigger_price_delta_pct'], 4)
            },
            'risk': {
                'margin_mode': 'isolated', 'risk_per_entry_pct': round(best_params_optuna['risk_per_entry_pct'], 2),
                'leverage': best_params_optuna['leverage'], 'stop_loss_pct': round(best_params_optuna['stop_loss_pct'], 2)
            },
            'behavior': {'use_longs': True, 'use_shorts': True}
        }
        final_result = run_envelope_backtest(HISTORICAL_DATA.copy(), final_params_dict, START_CAPITAL)

        final_pnl = final_result.get('total_pnl_pct', -1000)
        final_dd = final_result.get('max_drawdown_pct', 100)
        final_trades = final_result.get('trades_count', 0)
        final_win_rate = final_result.get('win_rate', 0)

        logger.info("\n--- Bestes Ergebnis ---")
        logger.info(f"  Score (PnL%): {best_score:.2f}%")
        logger.info(f"  Finaler PnL: {final_pnl:.2f}%")
        logger.info(f"  Finaler Max Drawdown: {final_dd:.2f}%")
        logger.info(f"  Trades: {final_trades}")
        logger.info(f"  Win Rate: {final_win_rate:.2f}%")
        logger.info(f"  Beste Parameter: {best_params_optuna}")

        # Sicherheitscheck
        if final_dd > (args.max_drawdown) or final_trades < MIN_TRADES_FOR_VALID:
             logger.warning(f"ACHTUNG: Das finale Ergebnis ({final_pnl:.1f}% PnL, {final_dd:.1f}% DD, {final_trades} Trades) "
                            f"erfüllt die Constraints (DD<{args.max_drawdown}%, Trades>={MIN_TRADES_FOR_VALID}) nicht mehr exakt.")

        optuna_results.append({
                'symbol': symbol, 'timeframe': timeframe, 'score': best_score,
                'pnl_pct': final_pnl, 'max_drawdown_pct': final_dd, 'win_rate': final_win_rate,
                'trades': final_trades, 'params': best_params_optuna, 'config_dict': final_params_dict
        })

        # --- Konfig speichern ---
        config_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
        os.makedirs(config_dir, exist_ok=True)
        config_filename = f'config_{safe_filename}{CONFIG_SUFFIX}.json'
        config_output_path = os.path.join(config_dir, config_filename)
        config_output = {
            "market": {"symbol": symbol, "timeframe": timeframe},
            "strategy": final_params_dict['strategy'],
            "risk": final_params_dict['risk'],
            "behavior": final_params_dict['behavior']
        }
        with open(config_output_path, 'w') as f: json.dump(config_output, f, indent=4)
        logger.info(f"✔ Beste Konfiguration wurde in '{config_output_path}' gespeichert.")


    # --- Zusammenfassung ---
    if optuna_results:
        logger.info("\n===== Optimierungs-Zusammenfassung =====")
        sorted_results = sorted(optuna_results, key=lambda x: x['score'], reverse=True)
        for res in sorted_results:
            logger.info(f"- {res['symbol']} ({res['timeframe']}): "
                        f"Score={res['score']:.2f}%, PnL={res['pnl_pct']:.2f}%, DD={res['max_drawdown_pct']:.2f}%, "
                        f"WR={res['win_rate']:.2f}%, Trades={res['trades']}")
    else:
        logger.warning("Keine erfolgreichen Optimierungsergebnisse zum Zusammenfassen.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
    main()
