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
from ltbbot.strategy.envelope_logic import median_atr_pct, sl_atr_fraction, band_structure_ok

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
# Fein-Kerzen (Bulk-Cache) auch WAEHREND der Suche: die Entry-Kerze wird seit 2026-10-03
# per Fein-Daten aufgeloest (simulate_entry_fill) -- ohne sie bevorzugt die Suche Baender,
# deren Gewinne nur aus der groben Close-Regel stammen (OOS-Portfolio +460% grob vs -1.8% fein).
SEARCH_FINE_DATA = None
# 'envelope' (alte Umkehr-Logik) oder 'breakout' (Band-Durchbruch, 2026-10-04, siehe strategy/breakout_logic.py)
STRATEGY_MODE = 'envelope'
OPTIM_MODE = "strict"
MIN_TRADES_FOR_VALID = 20       # wird pro Symbol proportional zur Trainingslänge berechnet
MIN_TRADES_PER_YEAR_GLOBAL = 20  # User-Eingabe in Trades/Jahr
SL_MAX_RATIO = 0.333             # Garantiert R:R ≥ 2:1 (sl_ratio = SL/env1, max 1/3)
IS_FRACTION = 0.70      # analog stbot/dnabot: 70% In-Sample, 30% Out-of-Sample
MIN_OOS_TRADES = 10     # Bestaetigung erfordert genug OOS-Trades fuer eine belastbare Aussage
MIN_OOS_PNL = 0.0  # Mindest-OOS-PnL in % (settings: min_oos_pnl_pct)
MIN_OOS_PROFIT_FACTOR = 1.3  # Bestaetigung erfordert winrate-unabhaengigen OOS-Profit-Faktor
                              # (Summe Gewinne / |Summe Verluste|) spuerbar ueber 1.0 -- siehe
                              # Docstring bei confirmed= weiter unten
K_FOLDS = 3              # IS-Teilfenster fuer den Robustheits-Score (siehe objective())
MIN_SL_ATR_FRACTION = 0.06  # Stop-Abstand Band 1 >= dieser Anteil der typischen Kerze (median ATR%),
                            # siehe envelope_logic.sl_atr_fraction; Wert aus settings.json
IS_ATR_PCT = None           # median ATR% des IS-Fensters des aktuellen Paars
MIN_ENV1_ATR = 0.5          # Band 1 >= 0.5 typische Kerzen vom MA (settings: min_env1_atr_fraction)
MIN_BAND_GAP_ATR = 0.25     # jede weitere Band-Luecke >= 0.25 Kerzen (settings: min_band_gap_atr_fraction)

def oos_gate(oos_result, min_trades, min_profit_factor, max_drawdown_decimal, min_pnl_pct=0.0):
    """OOS-Bestaetigungs-Kriterien (ohne Baseline-Vergleich) -- geteilt zwischen dem
    besten Trial und der Neubewertung einer bestehenden Config (2026-09-27), sowie
    von sync_confirmed_flags.py. Details zur Wahl der Kriterien siehe confirmed= in main().

    Returns: dict(passed, profit_factor, profit_factor_display, win_rate, max_dd_decimal)
    """
    trades = oos_result.get('trades', [])
    gross_wins = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gross_losses = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    if gross_losses > 0:
        profit_factor = gross_wins / gross_losses
    else:
        profit_factor = float('inf') if gross_wins > 0 else 0.0
    max_dd_decimal = oos_result.get('max_drawdown_pct', 100.0) / 100.0
    passed = bool(
        oos_result.get('trades_count', 0) >= min_trades
        and oos_result.get('total_pnl_pct', -1e9) > 0.0
        and oos_result.get('total_pnl_pct', -1e9) >= min_pnl_pct
        and profit_factor >= min_profit_factor
        and max_dd_decimal <= max_drawdown_decimal
    )
    return {
        'passed': passed,
        'profit_factor': profit_factor,
        # inf ist kein gueltiges JSON -- "keine Verlust-Trades" als grosser endlicher Platzhalter
        'profit_factor_display': min(profit_factor, 999.0),
        'win_rate': oos_result.get('win_rate', 0.0),
        'max_dd_decimal': max_dd_decimal,
    }


def create_safe_filename(symbol, timeframe):
    """Erstellt einen sicheren Dateinamen aus Symbol und Zeitrahmen."""
    return f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"

def _robust_score(params, trial):
    """K-Fold-Robustheit: schlechtestes IS-Teilfenster als Zielwert (wie Envelope)."""
    fold_size = len(IS_DATA) // K_FOLDS
    fold_pnls = []
    for k in range(K_FOLDS):
        fold_data = IS_DATA.iloc[k * fold_size: (k + 1) * fold_size if k < K_FOLDS - 1 else len(IS_DATA)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r = run_envelope_backtest(fold_data.copy(), params, START_CAPITAL, fine_data=SEARCH_FINE_DATA, multi_band_entries=True)
        fold_pnls.append(r.get('total_pnl_pct', -1000.0))
    trial.set_user_attr('fold_pnls', fold_pnls)
    return min(fold_pnls)


def _objective_breakout(trial):
    """Band-Durchbruch (2026-10-04): Long-Ausbruch ueber Durchschnitt + band_atr*ATR, SL/TP in ATR,
    Zeitstopp, BTC>SMA200-Filter fest an (praeregistriert bestaetigt), Shorts fest aus
    (in allen Regimen negativ, siehe docs/research/PREREG_breakout_long_2026-10-04.md)."""
    params = {
        'strategy': {
            'mode': 'breakout',
            'average_type': trial.suggest_categorical('average_type', ['SMA', 'EMA']),
            'average_period': trial.suggest_int('average_period', 10, 50),
            'atr_period': 14,
            'band_atr': round(trial.suggest_float('band_atr', 1.5, 4.5), 3),
            'max_hold_candles': trial.suggest_int('max_hold_candles', 20, 120),
            'use_btc_filter': True,
            'btc_sma_days': 200,
        },
        'risk': {
            'margin_mode': 'isolated',
            'sl_atr': round(trial.suggest_float('sl_atr', 1.0, 4.0), 3),
            'tp_r': round(trial.suggest_float('tp_r', 1.5, 6.0), 3),
            'risk_per_entry_pct': round(trial.suggest_float('risk_per_entry_pct', 0.5, 4.0), 2),
            'leverage': trial.suggest_int('leverage', 2, 10),
        },
        'behavior': {'use_longs': True, 'use_shorts': False},
    }
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = run_envelope_backtest(IS_DATA.copy(), params, START_CAPITAL, fine_data=SEARCH_FINE_DATA, multi_band_entries=True)
    if (result.get('max_drawdown_pct', 100.0) / 100.0 > MAX_DRAWDOWN_CONSTRAINT
            or result.get('trades_count', 0) < MIN_TRADES_FOR_VALID):
        raise optuna.exceptions.TrialPruned()
    trial.set_user_attr('params', params)
    trial.set_user_attr('envelopes', None)
    trial.set_user_attr('is_stats', {k: result.get(k) for k in ('total_pnl_pct', 'max_drawdown_pct', 'trades_count', 'win_rate')})
    return _robust_score(params, trial)


def objective(trial):
    """Optuna Objective-Funktion zur Optimierung der Envelope-Parameter."""
    global IS_DATA, START_CAPITAL, CURRENT_TIMEFRAME, OPTIM_MODE, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, MIN_TRADES_FOR_VALID, MIN_TRADES_PER_YEAR_GLOBAL, SL_MAX_RATIO, K_FOLDS

    if STRATEGY_MODE == 'breakout':
        return _objective_breakout(trial)

    # --- Parameter vorschlagen ---
    avg_type = trial.suggest_categorical('average_type', ['SMA', 'EMA', 'WMA', 'DCM'])
    avg_period = trial.suggest_int('average_period', 5, 50)
    # Baender in Einheiten der typischen Kerzenbewegung (IS median ATR%) statt in festen
    # Prozent (2026-09-30, siehe envelope_logic.band_structure_ok): passt sich jedem Coin
    # an -- BTC bekommt enge, hochvolatile Coins weite Baender. Gespeichert wird weiter
    # in Prozent (Live-Code unveraendert).
    env1_atr = trial.suggest_float('env1_atr', MIN_ENV1_ATR, 4.0)
    gap2_atr = trial.suggest_float('gap2_atr', MIN_BAND_GAP_ATR, 3.0)
    gap3_atr = trial.suggest_float('gap3_atr', MIN_BAND_GAP_ATR, 3.0)
    env1 = env1_atr * IS_ATR_PCT
    env2 = env1 + gap2_atr * IS_ATR_PCT
    env3 = env2 + gap3_atr * IS_ATR_PCT
    if env3 >= 0.5:
        raise optuna.exceptions.TrialPruned()
    envelopes = [env1, env2, env3]
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

    # Zu enge Stops im Verhaeltnis zur Kerzenbewegung verwerfen, bevor gebacktestet
    # wird (siehe envelope_logic.sl_atr_fraction) -- nur auf IS-Daten gemessen.
    _sl_frac = sl_atr_fraction(params, IS_ATR_PCT)
    if _sl_frac is not None and _sl_frac < MIN_SL_ATR_FRACTION:
        raise optuna.exceptions.TrialPruned()

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
        result = run_envelope_backtest(IS_DATA.copy(), params, START_CAPITAL, fine_data=SEARCH_FINE_DATA, multi_band_entries=True)

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
    trial.set_user_attr('envelopes', envelopes)
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
            fold_result = run_envelope_backtest(fold_data.copy(), params, START_CAPITAL, fine_data=SEARCH_FINE_DATA, multi_band_entries=True)
        fold_pnls.append(fold_result.get('total_pnl_pct', -1000.0))
    trial.set_user_attr('fold_pnls', fold_pnls)
    robust_score = min(fold_pnls)

    return robust_score


# --- Main Funktion ---
def main():
    global HISTORICAL_DATA, IS_DATA, OOS_DATA, CURRENT_SYMBOL, CURRENT_TIMEFRAME, CONFIG_SUFFIX, MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, START_CAPITAL, OPTIM_MODE, MIN_TRADES_FOR_VALID, MIN_TRADES_PER_YEAR_GLOBAL, SL_MAX_RATIO, IS_FRACTION, MIN_OOS_TRADES, MIN_OOS_PROFIT_FACTOR, K_FOLDS, MIN_SL_ATR_FRACTION, IS_ATR_PCT, MIN_ENV1_ATR, MIN_BAND_GAP_ATR

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
    parser.add_argument('--strategy_mode', type=str, default=None, choices=['envelope', 'breakout'],
                        help='Standard: settings.json optimization_settings.strategy_mode, sonst envelope')
    parser.add_argument('--min_oos_pnl', type=float, default=None,
                        help='Mindest-OOS-PnL in %% fuer Bestaetigung (Standard: settings.json min_oos_pnl_pct, sonst 0)')
    parser.add_argument('--min_oos_profit_factor', type=float, default=1.3,
                        help='Mindest-OOS-Profit-Faktor (Summe Gewinne / |Summe Verluste|, winrate-unabhaengig) '
                             'fuer eine Bestaetigung, Standard 1.3 -- verhindert Configs, die nur durch hauchduenn '
                             'positives OOS-PnL bestaetigt wurden (siehe research_ltbbot_live_vs_backtest_2026_09, '
                             '2026-09-25 -- bewusst Profit-Faktor statt Winrate, User-Vorgabe: profitabel '
                             'unabhaengig von der Winrate)')
    parser.add_argument('--min_sl_atr_fraction', type=float, default=None,
                        help='Stop-Abstand Band 1 >= Anteil der typischen Kerzenbewegung (median ATR%%). '
                             'Standard: settings.json::optimization_settings.min_sl_atr_fraction (0.06).')
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
    MIN_OOS_PROFIT_FACTOR = args.min_oos_profit_factor
    global MIN_OOS_PNL, STRATEGY_MODE
    if args.strategy_mode:
        STRATEGY_MODE = args.strategy_mode
    else:
        try:
            with open(os.path.join(PROJECT_ROOT, 'settings.json')) as _f:
                STRATEGY_MODE = json.load(_f).get('optimization_settings', {}).get('strategy_mode', 'envelope')
        except Exception:
            STRATEGY_MODE = 'envelope'
    if args.min_oos_pnl is not None:
        MIN_OOS_PNL = args.min_oos_pnl
    else:
        try:
            with open(os.path.join(PROJECT_ROOT, 'settings.json')) as _f:
                MIN_OOS_PNL = float(json.load(_f).get('optimization_settings', {}).get('min_oos_pnl_pct', 0.0))
        except Exception:
            MIN_OOS_PNL = 0.0
    K_FOLDS = args.k_folds
    if args.min_sl_atr_fraction is not None:
        MIN_SL_ATR_FRACTION = args.min_sl_atr_fraction
    else:
        try:
            with open(os.path.join(PROJECT_ROOT, 'settings.json')) as _sf:
                _os = json.load(_sf).get('optimization_settings', {})
            MIN_SL_ATR_FRACTION = float(_os.get('min_sl_atr_fraction', MIN_SL_ATR_FRACTION))
        except Exception:
            pass
    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json')) as _sf:
            _os = json.load(_sf).get('optimization_settings', {})
        MIN_ENV1_ATR = float(_os.get('min_env1_atr_fraction', MIN_ENV1_ATR))
        MIN_BAND_GAP_ATR = float(_os.get('min_band_gap_atr_fraction', MIN_BAND_GAP_ATR))
    except Exception:
        pass

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
        IS_ATR_PCT = median_atr_pct(IS_DATA)
        logger.info(f"Typische Kerzenbewegung (median ATR, IS): {IS_ATR_PCT*100:.2f}% -> "
                    f"Mindest-Stop Band 1: {MIN_SL_ATR_FRACTION*IS_ATR_PCT*100:.3f}%")
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

        # Fein-Daten EINMAL als Bulk-Cache laden und schon fuer die Suche nutzen (siehe SEARCH_FINE_DATA)
        global SEARCH_FINE_DATA
        SEARCH_FINE_DATA = None
        if fine_tf:
            try:
                SEARCH_FINE_DATA = load_data(symbol, fine_tf, args.start_date, args.end_date)
                if SEARCH_FINE_DATA is None or SEARCH_FINE_DATA.empty:
                    SEARCH_FINE_DATA = None
            except Exception as e:
                logger.warning(f"Fein-Daten ({fine_tf}) fuer die Suche nicht ladbar: {e} -- Suche mit grober Naeherung.")
                SEARCH_FINE_DATA = None

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
        if STRATEGY_MODE == 'breakout':
            final_params_dict = best_trial.user_attrs['params']
        else:
          final_params_dict = {
              'strategy': {
                  'average_type': best_params_optuna['average_type'], 'average_period': best_params_optuna['average_period'],
                  'envelopes': best_trial.user_attrs['envelopes'],
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
        fine_data_precise = SEARCH_FINE_DATA
        if fine_tf and fine_data_precise is None:
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
        #
        # VERSCHAERFT 2026-09-25 (siehe research_ltbbot_live_vs_backtest_2026_09):
        # "OOS-PnL > 0%" war trivial erfuellbar -- Median OOS/IS-Verhaeltnis ueber die
        # 12 damals aktiven Configs lag bei 0.10 (PEPE: IS=11167% vs. OOS=44%, Ratio
        # 0.004), UND die Live-WR blieb trotzdem bei ~0% ueber 43 Trades (p=1.5e-11
        # gegen die vom Backtest behauptete Edge). Die alte Schwelle bestaetigte
        # jeden Trial mit auch nur hauchduenn positivem OOS-PnL, unabhaengig von
        # tatsaechlicher Profitabilitaet oder OOS-Drawdown -- kein Schutz gegen genau
        # das Overfitting-Muster, das K_FOLDS eigentlich verhindern sollte.
        #
        # BEWUSST PROFIT-FAKTOR statt Winrate (User-Vorgabe 2026-09-25): "das soll
        # profitabel sein, unabhaengig von der Winrate" -- eine Strategie mit 25% WR
        # und starkem R:R kann profitabler sein als eine mit 60% WR und schwachem
        # R:R. Profit-Faktor (Summe Gewinne / |Summe Verluste| ueber die EINZELNEN
        # OOS-Trades, nicht die kumulierte %-Rendite) ist der winrate-unabhaengige
        # Standardmassstab dafuer und zusaetzlich robust gegen Compounding-Artefakte
        # in total_pnl_pct. >1.0 = profitabel, MIN_OOS_PROFIT_FACTOR gibt eine
        # Sicherheitsmarge fuer reale Kosten (Fees/Slippage), die der Backtest nur
        # approximiert.
        best_gate = oos_gate(best_oos, MIN_OOS_TRADES, MIN_OOS_PROFIT_FACTOR, MAX_DRAWDOWN_CONSTRAINT, MIN_OOS_PNL)
        if fine_tf and fine_data_precise is None and STRATEGY_MODE != 'breakout':
            # Ohne Fein-Daten greift die zu optimistische grobe Entry-Kerzen-Regel -> nie bestaetigen
            # (Breakout: Entry zum Open, ohne Fein-Daten gilt bei SL+TP in einer Kerze konservativ SL zuerst)
            logger.warning(f"Keine {fine_tf}-Feindaten fuer {symbol} ({timeframe}) -- Ergebnis NICHT bestaetigbar, Lauf spaeter wiederholen.")
            best_gate['passed'] = False
        oos_profit_factor_display = best_gate['profit_factor_display']
        oos_win_rate = best_gate['win_rate']  # 0-100 (Prozent) -- nur informativ, keine Gate-Bedingung
        oos_max_dd_decimal = best_gate['max_dd_decimal']
        confirmed = bool(
            best_gate['passed']
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
                        f"(OOS-Trades={best_oos.get('trades_count',0)}, OOS-PnL={best_oos.get('total_pnl_pct',0):+.2f}%, "
                        f"OOS-Profit-Faktor={oos_profit_factor_display:.2f} [Mindest {MIN_OOS_PROFIT_FACTOR:.2f}], "
                        f"OOS-WR={oos_win_rate:.1f}% (nur informativ), "
                        f"OOS-DD={oos_max_dd_decimal*100:.1f}% [Max {MAX_DRAWDOWN_CONSTRAINT*100:.0f}%]"
                        + (f", Baseline-OOS-PnL={baseline_oos.get('total_pnl_pct',0):+.2f}%" if baseline_oos is not None else "") + ").")
            run_results.setdefault('skipped', []).append({
                'symbol': symbol,
                'timeframe': timeframe,
                'new_oos_pnl_pct': round(best_oos.get('total_pnl_pct', 0), 2),
                'new_oos_profit_factor': round(oos_profit_factor_display, 2),
                'new_oos_win_rate': round(oos_win_rate, 2),
                'new_oos_max_drawdown_pct': round(oos_max_dd_decimal * 100, 2),
                'baseline_oos_pnl_pct': round(baseline_oos.get('total_pnl_pct', 0), 2) if baseline_oos is not None else None,
                'oos_trades': best_oos.get('trades_count', 0),
                'reason': 'oos_not_confirmed',
            })

            # Bestehende Config neu bewerten und ihren _meta-Status aktualisieren
            # (2026-09-27). Vorher blieb _meta.confirmed einer bestehenden Config fuer
            # immer auf dem Wert ihres Erstellungs-Laufs stehen -- nach dem Backtester-
            # Lookahead-Fix galten so 43/44 Configs weiter als "bestaetigt", obwohl sie
            # die OOS-Pruefung mit dem korrigierten Backtester nicht mehr bestanden, und
            # run_portfolio_optimizer.py waehlt nur unter bestaetigten Configs aus.
            if baseline_oos is not None:
                base_gate = oos_gate(baseline_oos, MIN_OOS_TRADES, MIN_OOS_PROFIT_FACTOR, MAX_DRAWDOWN_CONSTRAINT, MIN_OOS_PNL)
                if fine_tf and fine_data_precise is None and STRATEGY_MODE != 'breakout':
                    base_gate['passed'] = False
                if baseline_params['strategy'].get('mode') != 'breakout':
                    _base_frac = sl_atr_fraction(baseline_params, IS_ATR_PCT)
                    if _base_frac is not None and _base_frac < MIN_SL_ATR_FRACTION:
                        base_gate['passed'] = False
                    if not band_structure_ok(baseline_params, IS_ATR_PCT, MIN_ENV1_ATR, MIN_BAND_GAP_ATR):
                        base_gate['passed'] = False
                try:
                    existing_cfg.setdefault('_meta', {}).update({
                        'pnl_pct': round(baseline_is.get('total_pnl_pct', 0), 2),
                        'oos_pnl_pct': round(baseline_oos.get('total_pnl_pct', 0), 2),
                        'oos_trades': baseline_oos.get('trades_count', 0),
                        'oos_profit_factor': round(base_gate['profit_factor_display'], 2),
                        'oos_win_rate': round(base_gate['win_rate'], 2),
                        'oos_max_drawdown_pct': round(base_gate['max_dd_decimal'] * 100, 2),
                        'is_oos_split_date': str(split_ts.date()),
                        'is_fraction': IS_FRACTION,
                        'confirmed': base_gate['passed'],
                        'rechecked_at': _dt.now().isoformat(timespec='seconds'),
                    })
                    with open(config_output_path, 'w') as f:
                        json.dump(existing_cfg, f, indent=4)
                    logger.info(f"  Bestehende Config neu bewertet: confirmed={base_gate['passed']} "
                                f"(OOS-Trades={baseline_oos.get('trades_count', 0)}, "
                                f"OOS-PnL={baseline_oos.get('total_pnl_pct', 0):+.2f}%, "
                                f"PF={base_gate['profit_factor_display']:.2f}) -- Parameter unveraendert.")
                except Exception as e:
                    logger.warning(f"Konnte _meta der bestehenden Config nicht aktualisieren: {e}")
        else:
            config_output = {
                "_meta": {
                    "pnl_pct": round(final_pnl, 2),  # IS-PnL, Rueckwaertskompatibilitaet mit bestehendem Feld
                    "oos_pnl_pct": round(best_oos.get('total_pnl_pct', 0), 2),
                    "oos_trades": best_oos.get('trades_count', 0),
                    "oos_profit_factor": round(oos_profit_factor_display, 2),
                    "oos_win_rate": round(oos_win_rate, 2),
                    "oos_max_drawdown_pct": round(oos_max_dd_decimal * 100, 2),
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
