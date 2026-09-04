# src/ltbbot/analysis/optimizer.py
import os
import sys
import json
import optuna
import numpy as np
import argparse
import logging
import warnings
import contextlib
import io
from joblib import Parallel, delayed # Für Parallelisierung
from datetime import datetime as _dt

# Logging konfigurieren
# Optuna Logs auf WARNING reduzieren, um Balken nicht zu stören
logging.getLogger('optuna').setLevel(logging.WARNING)
optuna.logging.set_verbosity(optuna.logging.WARNING)

# Standard-Logger für dieses Skript
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

RESULTS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_optimizer_run.json')

# Verwende den Backtester für Envelope
from ltbbot.analysis.backtester import load_data, run_envelope_backtest, FINE_TF_MAP
from ltbbot.analysis.evaluator import evaluate_dataset

# Globale Variablen für die Objective-Funktion
HISTORICAL_DATA = None
IS_DATA = None    # vorderer Teil (IS_FRACTION) der Historie -- das sieht Optuna, wird optimiert
OOS_DATA = None   # hinterer Teil -- fliesst NIE in die Zielfunktion ein, nur Bestaetigung danach
CURRENT_SYMBOL = None
CURRENT_TIMEFRAME = None
CONFIG_SUFFIX = ""

# Constraints und Einstellungen
MAX_DRAWDOWN_CONSTRAINT = 0.30
MIN_WIN_RATE_CONSTRAINT = 0.0
MIN_PNL_CONSTRAINT = 0.0
START_CAPITAL = 1000
OPTIM_MODE = "strict"
MIN_TRADES_FOR_VALID = 20       # wird pro Symbol proportional zur Trainingslänge berechnet
MIN_TRADES_PER_YEAR_GLOBAL = 20  # User-Eingabe in Trades/Jahr
SL_MAX_RATIO = 0.333             # Garantiert R:R ≥ 2:1 (sl_ratio = SL/env1, max 1/3)
IS_FRACTION = 0.70      # analog stbot/dnabot: 70% In-Sample, 30% Out-of-Sample
MIN_OOS_TRADES = 10     # Bestaetigung erfordert genug OOS-Trades fuer eine belastbare Aussage
K_FOLDS = 3              # IS-Teilfenster fuer den Robustheits-Score (siehe objective())

def create_safe_filename(symbol, timeframe):
    """Erstellt einen sicheren Dateinamen aus Symbol und Zeitrahmen."""
    return f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"

def objective(trial):
    """Optuna Objective-Funktion zur Optimierung der Envelope-Parameter."""
    global IS_DATA, START_CAPITAL, CURRENT_TIMEFRAME, OPTIM_MODE, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, MIN_TRADES_FOR_VALID, MIN_TRADES_PER_YEAR_GLOBAL, SL_MAX_RATIO, K_FOLDS

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
    # SL als Anteil von env1 — garantiert R:R ≥ 2:1 (sl_ratio ≤ 0.333 → SL ≤ TP/2)
    sl_to_env1_ratio = trial.suggest_float('sl_to_env1_ratio', 0.10, SL_MAX_RATIO)

    # ADX-Regime-Gate (STRONG_TREND-Block bei ADX>30 sperrt Trading komplett):
    # Live-Trade-Forensik + fairer Backtest-Test ueber alle 6 Configs (2026-08-21,
    # siehe LIVE_TRADE_FORENSIK_2026-08.md) zeigten deutlichen Netto-Vorteil durch
    # Lockern/Deaktivieren des Blocks in 5/6 Configs (Summe-PnL +321% bei kompletter
    # Deaktivierung), ABER 1 Config (BNB) profitierte stattdessen von einem ENGEREN
    # Gate -- deshalb kein globaler Fixwert, sondern pro Symbol/Timeframe per Optuna.
    disable_strong_trend_block = trial.suggest_categorical('disable_strong_trend_block', [True, False])
    strong_trend_adx_threshold = (trial.suggest_float('strong_trend_adx_threshold', 20.0, 40.0)
                                   if not disable_strong_trend_block else 30.0)

    # --- Parameter-Dict ---
    params = {
        'strategy': {
            'average_type': avg_type, 'average_period': avg_period, 'envelopes': envelopes,
            'trigger_price_delta_pct': round(trigger_delta_pct, 4),
            'disable_strong_trend_block': disable_strong_trend_block,
            'strong_trend_adx_threshold': round(strong_trend_adx_threshold, 2),
        },
        'risk': {
            'margin_mode': 'isolated', 'risk_per_entry_pct': round(risk_per_entry_pct, 2),
            'leverage': leverage,
            'sl_to_env1_ratio': round(sl_to_env1_ratio, 4),
        },
        'behavior': {'use_longs': True, 'use_shorts': True}
    }

    # --- Backtest ---
    if IS_DATA is None or START_CAPITAL <= 0:
        # Verwende logger statt print im Objective
        # logger.error("...") # Wird durch backtester_logger unterdrückt, wenn auf ERROR gesetzt
        raise ValueError("IS_DATA oder START_CAPITAL nicht korrekt initialisiert.")

    # Zielfunktion sieht NUR IS-Daten -- OOS fliesst nie in die Optimierung ein,
    # nur in die Bestaetigung des besten Trials danach (siehe main()).
    # fine_data=None waehrend der SUCHE (grobe Naeherung statt LazyFineData-Intrabar-
    # Aufloesung, analog stbot/dnabot) -- ein einzelner Backtest MIT LazyFineData
    # brauchte hier gemessen >100s (viele einzelne Tages-Netzwerk-Fetches), OHNE nur
    # Bruchteile einer Sekunde. Bei 200 Trials ist das der Unterschied zwischen
    # Minuten und Stunden. Die praezisen Zahlen (fuer Tabelle + Config) kommen aus
    # einer einmaligen Nachbewertung des besten Trials nach der Suche, siehe main().
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = run_envelope_backtest(IS_DATA.copy(), params, START_CAPITAL, fine_data=None, multi_band_entries=True)

    # --- Ergebnisse ---
    pnl = result.get('total_pnl_pct', -1000.0)
    drawdown_pct_for_pruning = result.get('max_drawdown_pct', 100.0)
    drawdown_decimal_for_pruning = drawdown_pct_for_pruning / 100.0
    trades = result.get('trades_count', 0)
    win_rate = result.get('win_rate', 0.0)

    # --- Pruning (auf dem VOLLEN IS-Fenster -- genug Daten fuer eine verlaessliche
    # trades/Drawdown-Pruefung; einzelne K-Fold-Teilfenster waeren dafuer oft zu kurz) ---
    prune = False
    if OPTIM_MODE == "strict":
        if drawdown_decimal_for_pruning > MAX_DRAWDOWN_CONSTRAINT or win_rate < MIN_WIN_RATE_CONSTRAINT or pnl < MIN_PNL_CONSTRAINT or trades < MIN_TRADES_FOR_VALID:
            prune = True
    elif OPTIM_MODE == "best_profit":
        if drawdown_decimal_for_pruning > MAX_DRAWDOWN_CONSTRAINT or trades < MIN_TRADES_FOR_VALID:
            prune = True

    if prune:
        raise optuna.exceptions.TrialPruned()

    trial.set_user_attr('params', params)
    trial.set_user_attr('is_stats', result)

    # Robustheits-Score statt reiner Gesamt-IS-PnL: IS_DATA in K_FOLDS
    # aufeinanderfolgende Teilfenster splitten, jedes einzeln backtesten
    # (weiterhin fine_data=None, billig) und das SCHLECHTESTE Teilfenster als
    # Optuna-Zielwert nehmen. Reine Gesamt-PnL-Optimierung bevorzugt Parameter,
    # die eine einzelne Marktphase zufaellig gut treffen -- exakt das
    # Overfitting-Muster, das die 3-Monats-Analyse (LIVE_TRADE_FORENSIK_2026-08.md)
    # beim ADX-Gate aufgedeckt hat (in-sample besser, auf den juengsten 3 Monaten
    # netto schlechter). Das Minimum ueber mehrere Teilfenster bestraft das schon
    # WAEHREND der Suche, nicht erst hinterher im OOS-Check.
    fold_size = len(IS_DATA) // K_FOLDS
    fold_pnls = []
    for k in range(K_FOLDS):
        fold_data = IS_DATA.iloc[k * fold_size: (k + 1) * fold_size if k < K_FOLDS - 1 else len(IS_DATA)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fold_result = run_envelope_backtest(fold_data.copy(), params, START_CAPITAL, fine_data=None, multi_band_entries=True)
        fold_pnls.append(fold_result.get('total_pnl_pct', -1000.0))
    trial.set_user_attr('fold_pnls', fold_pnls)
    robust_score = min(fold_pnls)

    return robust_score


# --- Main Funktion ---
def main():
    global HISTORICAL_DATA, IS_DATA, OOS_DATA, CURRENT_SYMBOL, CURRENT_TIMEFRAME, CONFIG_SUFFIX, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, START_CAPITAL, OPTIM_MODE, MIN_TRADES_FOR_VALID, MIN_TRADES_PER_YEAR_GLOBAL, SL_MAX_RATIO, IS_FRACTION, MIN_OOS_TRADES, K_FOLDS

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
    parser.add_argument('--min_trades_per_year', type=int, default=20,
                        help='Mindest-Trades pro Jahr (proportional auf Trainingslänge skaliert)')
    # IS/OOS-Split + K-Fold-Robustheit (Port von stbot/analysis/optimizer.py, 2026-08-21) --
    # behebt das In-Sample-Overfitting, das die 3-Monats-Analyse beim ADX-Gate aufdeckte
    # (voller 3-Jahres-Backtest: Gate AUS besser; juengste 3 Monate allein: Gate AUS schlechter).
    parser.add_argument('--is_fraction', type=float, default=0.70,
                        help='Anteil In-Sample (Rest ist Out-of-Sample-Validierung), Standard 0.70')
    parser.add_argument('--min_oos_trades', type=int, default=10,
                        help='Mindestanzahl OOS-Trades fuer eine belastbare Bestaetigung, Standard 10')
    parser.add_argument('--k_folds', type=int, default=3,
                        help='Anzahl IS-Teilfenster fuer den Robustheits-Score (Minimum ueber alle Fenster), Standard 3')
    # Re-Optimierungs-Sperre (Port von dnabots alphabet_optimizer.py-Muster,
    # User-Anforderung 2026-08-26: "Overfeeding" vermeiden, wenn wiederholte
    # Laeufe (z.B. woechentlicher Scheduler oder mehrfach gestartete Sweeps)
    # ein bereits bestaetigtes Paar immer wieder neu fitten wollen).
    parser.add_argument('--recheck-confirmed', action='store_true',
                        help='Bereits bestaetigte Configs (_meta.confirmed=true) trotzdem neu optimieren. '
                             'Standard: werden uebersprungen, um wiederholtes Overfeeding auf denselben Daten zu vermeiden.')
    parser.add_argument('--recheck-after-days', type=int, default=7,
                        help='Unabhaengig von --recheck-confirmed: JEDES Paar (bestaetigt oder nicht) wird '
                             'uebersprungen, wenn seine Config vor weniger als N Tagen optimiert wurde. 0 deaktiviert die Sperre.')
    parser.add_argument('--results_file', type=str, default=None,
                        help='Alternativer Pfad statt artifacts/results/last_optimizer_run.json -- wichtig fuer '
                             'Ad-hoc-/Screening-Laeufe (z.B. viele Symbole mit wenigen Trials), damit deren Ergebnisse '
                             'NICHT in die Datei fliessen, die master_runner.py als Live-Trading-Fallback liest, '
                             'falls active_strategies mal leer ist.')
    args = parser.parse_args()
    results_file = args.results_file or RESULTS_FILE

    # Globale Variablen setzen
    CONFIG_SUFFIX = args.config_suffix
    MAX_DRAWDOWN_CONSTRAINT = args.max_drawdown / 100.0
    MIN_WIN_RATE_CONSTRAINT = args.min_win_rate
    MIN_PNL_CONSTRAINT = args.min_pnl
    START_CAPITAL = args.start_capital
    OPTIM_MODE = args.mode
    N_TRIALS = args.trials
    MIN_TRADES_PER_YEAR_GLOBAL = args.min_trades_per_year
    IS_FRACTION = args.is_fraction
    MIN_OOS_TRADES = args.min_oos_trades
    K_FOLDS = args.k_folds

    symbols, timeframes = args.symbols.split(), args.timeframes.split()
    TASKS = [{'symbol': f"{s.upper()}/USDT:USDT", 'timeframe': tf} for s in symbols for tf in timeframes]

    optuna_results = []

    # last_optimizer_run.json: lesen falls vorhanden (Scheduler initialisiert vor Pipeline-Start)
    os.makedirs(os.path.dirname(results_file), exist_ok=True)
    if os.path.exists(results_file):
        try:
            with open(results_file, 'r', encoding='utf-8') as f:
                run_results = json.load(f)
            run_results.setdefault('saved', [])
            run_results.setdefault('skipped', [])
            run_results.setdefault('failed', [])
        except Exception:
            run_results = {'run_start': _dt.now().isoformat(timespec='seconds'), 'run_end': None, 'saved': [], 'skipped': [], 'failed': []}
    else:
        run_results = {'run_start': _dt.now().isoformat(timespec='seconds'), 'run_end': None, 'saved': [], 'skipped': [], 'failed': []}

    for task in TASKS:
        symbol, timeframe = task['symbol'], task['timeframe']
        CURRENT_SYMBOL = symbol
        CURRENT_TIMEFRAME = timeframe

        logger.info(f"\n===== Optimiere: {symbol} ({timeframe}) {CONFIG_SUFFIX} =====")

        # --- Re-Optimierungs-Sperre: bereits bestaetigte/kuerzlich gefittete
        # Configs ueberspringen, statt sie bei jedem Lauf erneut zu ueberbuegeln
        # (Overfeeding-Schutz, analog dnabot alphabet_optimizer.py) ---
        _safe_fn = create_safe_filename(symbol, timeframe)
        _existing_cfg_path = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs',
                                           f'config_{_safe_fn}{CONFIG_SUFFIX}.json')
        if os.path.exists(_existing_cfg_path):
            try:
                with open(_existing_cfg_path, 'r', encoding='utf-8') as _cf:
                    _existing_meta = json.load(_cf).get('_meta', {})
            except Exception:
                _existing_meta = {}
            _is_confirmed = bool(_existing_meta.get('confirmed'))
            _optimized_at_str = _existing_meta.get('optimized_at')
            _age_days = None
            if _optimized_at_str:
                try:
                    _age_days = (_dt.now() - _dt.fromisoformat(_optimized_at_str)).days
                except Exception:
                    _age_days = None

            if _is_confirmed and not args.recheck_confirmed:
                logger.info(f"⏭ Uebersprungen: {symbol} ({timeframe}) ist bereits bestaetigt "
                            f"(optimiert am {_optimized_at_str or 'unbekannt'}) -- --recheck-confirmed erzwingt eine Neupruefung.")
                run_results['skipped'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'already_confirmed_locked'})
                continue
            if args.recheck_after_days > 0 and _age_days is not None and _age_days < args.recheck_after_days:
                logger.info(f"⏭ Uebersprungen: {symbol} ({timeframe}) wurde vor {_age_days} Tag(en) optimiert "
                            f"(Sperre: {args.recheck_after_days} Tage) -- verhindert zu haeufiges Re-Fitting.")
                run_results['skipped'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'recheck_cooldown'})
                continue

        # --- Daten laden ---
        try:
            HISTORICAL_DATA = load_data(symbol, timeframe, args.start_date, args.end_date)
            if HISTORICAL_DATA is None or HISTORICAL_DATA.empty:
                logger.warning(f"Keine Daten für {symbol} ({timeframe}) geladen. Überspringe.")
                run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'no_data'})
                continue
        except Exception as e:
            logger.error(f"Fehler beim Laden der Daten für {symbol} ({timeframe}): {e}", exc_info=True)
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'no_data'})
            continue

        # Chronologischer IS/OOS-Split (Port von stbot/analysis/optimizer.py): die ersten
        # IS_FRACTION der Kerzen sieht Optuna (Zielfunktion), der Rest dient ausschliesslich
        # der spaeteren Bestaetigung des besten Trials -- fliesst nie in die Suche ein.
        split_idx = int(len(HISTORICAL_DATA) * IS_FRACTION)
        split_ts  = HISTORICAL_DATA.index[split_idx]
        IS_DATA   = HISTORICAL_DATA.iloc[:split_idx]
        OOS_DATA  = HISTORICAL_DATA.iloc[split_idx:]
        logger.info(
            f"{symbol} ({timeframe}): {len(HISTORICAL_DATA)} Kerzen | "
            f"IS bis {split_ts.date()} ({split_idx} Kerzen) | "
            f"OOS ab {split_ts.date()} ({len(HISTORICAL_DATA) - split_idx} Kerzen)"
        )

        # Feinere Kerzen fuer SL/TP-Intrabar-Reihenfolgen-Aufloesung (oraclebot-Muster) --
        # NUR fuer die einmalige Praezisions-Nachbewertung des besten Trials nach der Suche
        # (siehe unten). Waehrend der Suche selbst nutzt objective() bewusst fine_data=None.
        fine_tf = FINE_TF_MAP.get(timeframe)

        # --- Proportionale min_trades Berechnung (wie titanbot), auf dem IS-Fenster --
        # objective() prueft die Trade-Anzahl-Constraint ausschliesslich auf IS_DATA, also
        # muss die Skalierung auch auf dessen Laenge basieren (vorher: volle Historie).
        _train_days = max(1, (IS_DATA.index[-1] - IS_DATA.index[0]).days)
        MIN_TRADES_FOR_VALID = max(2, int(MIN_TRADES_PER_YEAR_GLOBAL * _train_days / 365))
        logger.info(f"Mindest-Trades (IS-Fenster): >={MIN_TRADES_FOR_VALID} ({_train_days}d @ {MIN_TRADES_PER_YEAR_GLOBAL}/Jahr)")

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
        safe_filename = create_safe_filename(symbol, timeframe)
        study_name = f"{safe_filename}{CONFIG_SUFFIX}_{OPTIM_MODE}"

        # Vor dem Optimize-Aufruf Logger holen und Level merken/setzen
        # Unterdrücke alle per-Kerzen-Logs während der Optimierung
        _noisy_loggers = [
            logging.getLogger('ltbbot.analysis.backtester'),
            logging.getLogger('ltbbot.strategy.envelope_logic'),
            logging.getLogger('ltbbot.strategy.envelope_detector'),
            logging.getLogger('ltbbot.utils.exchange'),
        ]
        _original_levels = [lg.level for lg in _noisy_loggers]
        for lg in _noisy_loggers:
            lg.setLevel(logging.ERROR)

        try:
            study = optuna.create_study(study_name=study_name, direction="maximize")

            n_jobs = args.jobs
            logger.info(f"Starte Optuna-Optimierung mit {N_TRIALS} Trials und {n_jobs} Job(s)...")

            from tqdm import tqdm as _tqdm
            _bar = _tqdm(total=N_TRIALS, desc=f'  {symbol} ({timeframe})',
                         unit='trial', leave=True, dynamic_ncols=True)
            def _progress_cb(study, trial):
                _bar.update(1)

            study.optimize(
                objective,
                n_trials=N_TRIALS,
                n_jobs=n_jobs,
                show_progress_bar=False,
                callbacks=[_progress_cb],
            )
            _bar.close()

        except Exception as e:
            logger.error(f"Schwerwiegender Fehler während der Optuna-Studie für {symbol} ({timeframe}): {e}", exc_info=True)
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': f'study_error: {str(e)[:100]}'})
            continue # Nächsten Task versuchen
        finally:
            # Stelle sicher, dass Level immer zurückgesetzt wird
            for lg, lvl in zip(_noisy_loggers, _original_levels):
                lg.setLevel(lvl)


        # --- Bestes Ergebnis ---
        valid_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if not valid_trials:
            logger.error(f"\n❌ FEHLER: Für {symbol} ({timeframe}) konnte keine gültige Konfiguration gefunden werden.")
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'no_valid_trials'})
            continue

        best_trial = study.best_trial
        best_params_optuna = best_trial.params
        best_score = best_trial.value  # K-Fold-Robustheits-Score (Minimum ueber IS-Teilfenster), NICHT roh-PnL

        # Finale Parameter aus dem besten Trial
        final_params_dict = {
            'strategy': {
                'average_type': best_params_optuna['average_type'], 'average_period': best_params_optuna['average_period'],
                'envelopes': sorted([best_params_optuna['env1'], best_params_optuna['env2'], best_params_optuna['env3']]),
                'trigger_price_delta_pct': round(best_params_optuna['trigger_price_delta_pct'], 4),
                'disable_strong_trend_block': best_params_optuna.get('disable_strong_trend_block', False),
                'strong_trend_adx_threshold': round(best_params_optuna.get('strong_trend_adx_threshold', 30.0), 2),
            },
            'risk': {
                'margin_mode': 'isolated', 'risk_per_entry_pct': round(best_params_optuna['risk_per_entry_pct'], 2),
                'leverage': best_params_optuna['leverage'],
                'sl_to_env1_ratio': round(best_params_optuna['sl_to_env1_ratio'], 4),
            },
            'behavior': {'use_longs': True, 'use_shorts': True}
        }

        # Praezise Nachbewertung NUR des besten Trials mit echter Intrabar-Aufloesung --
        # waehrend der Suche liefen alle Trials bewusst mit fine_data=None (siehe objective()).
        #
        # WICHTIG: hier bewusst EIN grosser Bulk-Fetch (load_data, wie im normalen Backtest-
        # Modus) statt LazyFineData -- LazyFineData holt Tag fuer Tag einzeln, was fuer einen
        # durchgehenden Nachbewertungs-Lauf ueber Monate/Jahre viel zu viele Einzel-Requests
        # bedeutet (stbot-Messung: >12 Min fuer ein 3-Jahres-Fenster, >80% reine Netzwerk-
        # Wartezeit). Ein einziger zusammenhaengender Bulk-Fetch nutzt Bitgets 200-Kerzen-
        # Pagination viel effizienter und landet in einem wiederverwendbaren Cache.
        fine_data_precise = None
        if fine_tf:
            try:
                fine_data_precise = load_data(symbol, fine_tf, args.start_date, args.end_date)
                if fine_data_precise is None or fine_data_precise.empty:
                    fine_data_precise = None
            except Exception as e:
                logger.warning(f"Praezise Fein-Daten ({fine_tf}) konnten nicht geladen werden: {e}. Nachbewertung ohne Intrabar-Aufloesung.")
                fine_data_precise = None

        best_is  = run_envelope_backtest(IS_DATA.copy(), final_params_dict, START_CAPITAL, fine_data=fine_data_precise, multi_band_entries=True)
        best_oos = run_envelope_backtest(OOS_DATA.copy(), final_params_dict, START_CAPITAL, fine_data=fine_data_precise, multi_band_entries=True)

        final_pnl = best_is.get('total_pnl_pct', best_score)
        final_dd = best_is.get('max_drawdown_pct', 100)
        final_trades = best_is.get('trades_count', 0)
        final_win_rate = best_is.get('win_rate', 0)

        logger.info("\n--- Bestes Ergebnis ---")
        logger.info(f"  Score (K-Fold-Robustheit): {best_score:.2f}%")
        fold_pnls = best_trial.user_attrs.get('fold_pnls')
        if fold_pnls:
            logger.info(f"  IS-Teilfenster ({K_FOLDS}x): " + " / ".join(f"{p:+.1f}%" for p in fold_pnls))
        logger.info(f"  IS  PnL: {final_pnl:.2f}%  | DD: {final_dd:.2f}%  | Trades: {final_trades}  | WR: {final_win_rate:.2f}%")
        logger.info(f"  OOS PnL: {best_oos.get('total_pnl_pct', 0):.2f}%  | DD: {best_oos.get('max_drawdown_pct', 0):.2f}%  | "
                    f"Trades: {best_oos.get('trades_count', 0)}  | WR: {best_oos.get('win_rate', 0):.2f}%")
        logger.info(f"  Beste Parameter: {best_params_optuna}")

        # Sicherheitscheck (auf IS-Fenster, wie bisher)
        if final_dd > (args.max_drawdown) or final_trades < MIN_TRADES_FOR_VALID:
             logger.warning(f"ACHTUNG: Das finale IS-Ergebnis ({final_pnl:.1f}% PnL, {final_dd:.1f}% DD, {final_trades} Trades) "
                            f"erfüllt die Constraints (DD<{args.max_drawdown}%, Trades>={MIN_TRADES_FOR_VALID}) nicht mehr exakt.")

        optuna_results.append({
                'symbol': symbol, 'timeframe': timeframe, 'score': best_score,
                'pnl_pct': final_pnl, 'max_drawdown_pct': final_dd, 'win_rate': final_win_rate,
                'trades': final_trades, 'oos_pnl_pct': best_oos.get('total_pnl_pct', 0),
                'oos_trades': best_oos.get('trades_count', 0),
                'params': best_params_optuna, 'config_dict': final_params_dict
        })

        # --- Konfig speichern (nur wenn OOS-bestaetigt besser als bestehende) ---
        config_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
        os.makedirs(config_dir, exist_ok=True)
        config_filename = f'config_{safe_filename}{CONFIG_SUFFIX}.json'
        config_output_path = os.path.join(config_dir, config_filename)

        # Baseline = bestehende Config (falls vorhanden), auf DENSELBEN IS/OOS-Daten
        # ausgewertet -- das ist der "Ist-Zustand", gegen den der beste Trial bestaetigt
        # werden muss (Port von stbot: OOS-Vergleich statt _meta.pnl_pct-Vergleich, weil
        # _meta.pnl_pct vor dem Port ueber die VOLLE Historie lief und daher nicht direkt
        # mit einem IS-only-Wert vergleichbar ist).
        baseline_is, baseline_oos = None, None
        if os.path.exists(config_output_path):
            try:
                with open(config_output_path, 'r') as cf:
                    existing_cfg = json.load(cf)
                baseline_params = {
                    'strategy': dict(existing_cfg['strategy']),
                    'risk': dict(existing_cfg['risk']),
                    'behavior': dict(existing_cfg.get('behavior', {'use_longs': True, 'use_shorts': True})),
                }
                baseline_is  = run_envelope_backtest(IS_DATA.copy(), baseline_params, START_CAPITAL, fine_data=fine_data_precise, multi_band_entries=True)
                baseline_oos = run_envelope_backtest(OOS_DATA.copy(), baseline_params, START_CAPITAL, fine_data=fine_data_precise, multi_band_entries=True)
            except Exception as e:
                logger.warning(f"Baseline-Bewertung fehlgeschlagen ({e}) -- werte ohne Baseline-Vergleich.")
                baseline_is, baseline_oos = None, None

        # Bestaetigung: genug OOS-Trades fuer eine belastbare Aussage, OOS-PnL positiv,
        # UND (falls Baseline vorhanden) besser als die bestehende Config auf denselben
        # OOS-Daten. bool(...) um den Gesamtausdruck: numpy.bool_-Operanden (total_pnl_pct
        # kommt aus run_envelope_backtest(), pandas/numpy-basiert) sind sonst nicht
        # json-serialisierbar (siehe identischer Bug/Fix in stbot 2026-08-21).
        confirmed = bool(
            best_oos.get('trades_count', 0) >= MIN_OOS_TRADES
            and best_oos.get('total_pnl_pct', -1e9) > 0.0
            and (baseline_oos is None or best_oos.get('total_pnl_pct', -1e9) > baseline_oos.get('total_pnl_pct', -1e9))
        )

        mark = '[BESTAETIGT]' if confirmed else '[nicht bestaetigt]'
        logger.info(f"\n  --- {symbol} ({timeframe}) --- {mark}")
        if baseline_is is not None:
            logger.info(f"  {'Metrik':<10}{'Baseline IS':>13}{'Best IS':>11}   |{'Baseline OOS':>14}{'Best OOS':>11}")
            logger.info(f"  {'Trades':<10}{baseline_is.get('trades_count',0):>13}{best_is.get('trades_count',0):>11}   |"
                        f"{baseline_oos.get('trades_count',0):>14}{best_oos.get('trades_count',0):>11}")
            logger.info(f"  {'WinRate':<10}{baseline_is.get('win_rate',0):>12.1f}%{best_is.get('win_rate',0):>10.1f}%   |"
                        f"{baseline_oos.get('win_rate',0):>13.1f}%{best_oos.get('win_rate',0):>10.1f}%")
            logger.info(f"  {'PnL %':<10}{baseline_is.get('total_pnl_pct',0):>+12.1f}%{best_is.get('total_pnl_pct',0):>+10.1f}%   |"
                        f"{baseline_oos.get('total_pnl_pct',0):>+13.1f}%{best_oos.get('total_pnl_pct',0):>+10.1f}%")
            logger.info(f"  {'MaxDD %':<10}{baseline_is.get('max_drawdown_pct',0):>12.1f}%{best_is.get('max_drawdown_pct',0):>10.1f}%   |"
                        f"{baseline_oos.get('max_drawdown_pct',0):>13.1f}%{best_oos.get('max_drawdown_pct',0):>10.1f}%")
        else:
            logger.info("  (kein bestehender Config zum Vergleich -- erster Lauf fuer dieses Paar)")

        if not confirmed:
            logger.info(f"⏭ Konfiguration NICHT gespeichert/aktualisiert: OOS-Bestaetigung nicht erfolgreich "
                        f"(OOS-Trades={best_oos.get('trades_count',0)}, OOS-PnL={best_oos.get('total_pnl_pct',0):+.2f}%"
                        + (f", Baseline-OOS-PnL={baseline_oos.get('total_pnl_pct',0):+.2f}%" if baseline_oos is not None else "") + ").")
            run_results.setdefault('skipped', []).append({
                'symbol': symbol,
                'timeframe': timeframe,
                'new_oos_pnl_pct': round(best_oos.get('total_pnl_pct', 0), 2),
                'baseline_oos_pnl_pct': round(baseline_oos.get('total_pnl_pct', 0), 2) if baseline_oos is not None else None,
                'oos_trades': best_oos.get('trades_count', 0),
                'reason': 'oos_not_confirmed',
            })
        else:
            config_output = {
                "_meta": {
                    "pnl_pct": round(final_pnl, 2),  # IS-PnL, Rueckwaertskompatibilitaet mit bestehendem Feld
                    "oos_pnl_pct": round(best_oos.get('total_pnl_pct', 0), 2),
                    "oos_trades": best_oos.get('trades_count', 0),
                    "is_oos_split_date": str(split_ts.date()),
                    "is_fraction": IS_FRACTION,
                    "k_folds": K_FOLDS,
                    "confirmed": confirmed,
                    "optimized_at": _dt.now().isoformat(timespec='seconds'),
                },
                "market": {"symbol": symbol, "timeframe": timeframe},
                "strategy": final_params_dict['strategy'],
                "risk": final_params_dict['risk'],
                "behavior": final_params_dict['behavior']
            }
            with open(config_output_path, 'w') as f: json.dump(config_output, f, indent=4)
            logger.info(f"✔ Konfiguration gespeichert (OOS-bestaetigt, PnL IS={final_pnl:.2f}% / OOS={best_oos.get('total_pnl_pct', 0):.2f}%): '{config_output_path}'")

            run_results['saved'].append({
                'symbol': symbol,
                'timeframe': timeframe,
                'pnl_pct': round(final_pnl, 2),
                'oos_pnl_pct': round(best_oos.get('total_pnl_pct', 0), 2),
                'confirmed': confirmed,
                'config_file': config_filename,
            })


    # --- Ergebnisdatei aktualisieren (results_file, default last_optimizer_run.json) ---
    run_results['run_end'] = _dt.now().isoformat(timespec='seconds')
    try:
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(run_results, f, indent=2)
    except Exception as e:
        logger.warning(f"Konnte {results_file} nicht schreiben: {e}")

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
