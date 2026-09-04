#!/usr/bin/env python3
"""
screen_volatility.py

Schnelle Vorfilterung OHNE Optuna/Backtest: berechnet fuer jedes Kandidaten-
Symbol/Timeframe reine Kerzen-Kennzahlen (Regime-Verteilung, Volatilitaet,
Envelope-Beruehrungshaeufigkeit) und vergleicht sie mit denselben Kennzahlen
der 13 aktuell aktiven, bestaetigten Strategien (der "bekannt gute" Referenz-
Cluster). Kandidaten, die diesem Profil aehneln, sind wahrscheinlicher gute
Envelope-Mean-Reversion-Kandidaten -- OHNE dass dafuer ein einziger Optuna-
Trial/Backtest laufen muss. Braucht nur OHLCV-Daten + Pandas/TA-Rechnungen,
daher Minuten statt Stunden fuer hunderte Symbole.

Kennzahlen (identische Regime-Logik wie envelope_logic.py/backtester.py):
  - range_pct / trend_pct / strong_trend_pct: Zeitanteil in jedem ADX-Regime
    (RANGE ist fuer Mean-Reversion guenstig, STRONG_TREND blockiert Trading)
  - atr_pct: durchschnittliche ATR als % vom Preis (Volatilitaet)
  - touches_per_week: wie oft pro Woche wuerde ein generisches 2%-Envelope-
    Band mit Close-Confirmation beruehrt (Schaetzung der Signalhaeufigkeit)

Danach: Distanz jedes Kandidaten zum Median-Profil der Referenz-Strategien
(z-normalisiert je Kennzahl) -- kleinste Distanz = aehnlichstes Profil = mit
hoher Prioritaet fuer die (teure) volle Pipeline / screen_candidates.py.

Aufruf:
  python screen_volatility.py                     # alle aktiven USDT-Perpetuals
  python screen_volatility.py --top-n 200          # nur Top 200 nach Volumen
  python screen_volatility.py --timeframes "1h 4h" # andere Timeframes
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd
import ta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange  # noqa: E402
from ltbbot.analysis.backtester import load_data  # noqa: E402

DEFAULT_TIMEFRAMES = ['30m', '1h', '2h', '4h', '6h']
CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
CSV_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'screen_volatility.csv')

METRIC_COLS = ['range_pct', 'trend_pct', 'strong_trend_pct', 'atr_pct', 'touches_per_week']


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def compute_fit_stats(df: pd.DataFrame, avg_period: int, env_pct: float,
                       trigger_delta_pct: float = 0.0005) -> dict | None:
    """Reine Kennzahlen aus OHLCV, keine Optimierung, kein Backtest.
    Regime-Klassifikation identisch zu backtester.py's vorab-berechneten
    Arrays (ADX>30=STRONG_TREND, >25=TREND, <20 & dist<3%=RANGE, sonst
    UNCERTAIN)."""
    if df is None or len(df) < max(avg_period, 50) + 10:
        return None
    close, high, low = df['close'], df['high'], df['low']

    average = ta.trend.sma_indicator(close, window=avg_period)
    adx = ta.trend.adx(high, low, close, window=14)
    atr = ta.volatility.average_true_range(high, low, close, window=14)
    price_dist_pct = (close - average).abs() / average * 100

    valid = average.notna() & adx.notna() & atr.notna()
    n_valid = int(valid.sum())
    if n_valid < 50:
        return None

    strong_trend = (adx > 30) & valid
    trend = (adx > 25) & (~strong_trend) & valid
    range_regime = (adx < 20) & (price_dist_pct < 3) & valid

    band_low = average * (1 - env_pct)
    band_high = average * (1 + env_pct)
    entry_trigger_low = band_low * (1 - trigger_delta_pct)
    entry_trigger_high = band_high * (1 + trigger_delta_pct)
    prev_close = close.shift(1)
    prev_band_low = band_low.shift(1)
    prev_band_high = band_high.shift(1)

    long_touch = (low <= entry_trigger_low) & (prev_close <= prev_band_low) & valid
    short_touch = (high >= entry_trigger_high) & (prev_close >= prev_band_high) & valid
    total_touches = int(long_touch.sum() + short_touch.sum())

    span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400.0
    weeks = max(span_days / 7.0, 1.0)

    return {
        'range_pct': round(range_regime.sum() / n_valid * 100, 2),
        'trend_pct': round(trend.sum() / n_valid * 100, 2),
        'strong_trend_pct': round(strong_trend.sum() / n_valid * 100, 2),
        'atr_pct': round(float((atr / close)[valid].mean() * 100), 4),
        'touches_per_week': round(total_touches / weeks, 3),
        'n_candles': n_valid,
    }


def build_reference_profile(lookback_weeks: int) -> pd.DataFrame:
    """Kennzahlen der 13 aktuell aktiven Strategien, jeweils mit ihrem
    EIGENEN average_period/envelope[0] aus der echten Config -- das ist der
    'bekannt gute' Vergleichs-Cluster."""
    with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
        settings = json.load(f)
    active = [s for s in settings.get('live_trading_settings', {}).get('active_strategies', [])
              if s.get('active')]

    end_date = date.today().strftime('%Y-%m-%d')
    start_date = (date.today() - timedelta(weeks=lookback_weeks)).strftime('%Y-%m-%d')

    rows = []
    for s in active:
        symbol, timeframe = s['symbol'], s['timeframe']
        coin = symbol.split('/')[0]
        cfg_files = [f for f in os.listdir(CONFIGS_DIR)
                     if f.startswith(f"config_{coin}USDTUSDT_{timeframe}_") and f.endswith('_envelope.json')]
        if not cfg_files:
            continue
        with open(os.path.join(CONFIGS_DIR, cfg_files[0])) as f:
            cfg = json.load(f)
        avg_period = cfg['strategy']['average_period']
        env1 = cfg['strategy']['envelopes'][0]
        df = load_data(symbol, timeframe, start_date, end_date)
        stats = compute_fit_stats(df, avg_period, env1)
        if stats:
            stats.update({'symbol': symbol, 'timeframe': timeframe,
                          'avg_period': avg_period, 'env1': env1})
            rows.append(stats)
            _log(f"Referenz {symbol} ({timeframe}): {stats}")
    return pd.DataFrame(rows)


def rank_candidates(ref_df: pd.DataFrame, cand_df: pd.DataFrame) -> pd.DataFrame:
    """Z-normalisierte euklidische Distanz jedes Kandidaten zum Median-Profil
    der Referenz-Strategien -- kleinste Distanz = aehnlichstes Profil."""
    ref_median = ref_df[METRIC_COLS].median()
    ref_std = ref_df[METRIC_COLS].std().replace(0, 1.0)

    z = (cand_df[METRIC_COLS] - ref_median) / ref_std
    cand_df = cand_df.copy()
    cand_df['fit_distance'] = np.sqrt((z ** 2).sum(axis=1))
    return cand_df.sort_values('fit_distance')


def main():
    parser = argparse.ArgumentParser(description="Schnelles Volatilitaets-/Regime-Screening (kein Optuna)")
    parser.add_argument('--top-n', type=int, default=None, help='Nur Top N nach 24h-Volumen (Default: alle aktiven)')
    parser.add_argument('--timeframes', type=str, default=' '.join(DEFAULT_TIMEFRAMES))
    parser.add_argument('--lookback-weeks', type=int, default=16)
    parser.add_argument('--workers', type=int, default=8, help='Parallele Threads fuer Datenabruf (I/O-gebunden)')
    args = parser.parse_args()
    timeframes = args.timeframes.split()

    with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
        secrets = json.load(f)
    ex = Exchange(secrets['ltbbot'][0])

    _log("Baue Referenz-Profil aus den 13 aktiven Strategien...")
    ref_df = build_reference_profile(args.lookback_weeks)
    if ref_df.empty:
        _log("FEHLER: Kein Referenz-Profil berechenbar (keine aktiven Configs/Daten gefunden).")
        return
    _log(f"Referenz-Median: {ref_df[METRIC_COLS].median().to_dict()}")

    tickers = ex.exchange.fetch_tickers(params={'productType': 'USDT-FUTURES'})
    active_symbols = {
        m['symbol'] for m in ex.markets.values()
        if m.get('swap') and m.get('quote') == 'USDT' and m.get('settle') == 'USDT' and m.get('active', True)
    }
    vol_rows = [(sym, t.get('quoteVolume') or 0.0) for sym, t in tickers.items() if sym in active_symbols]
    vol_rows.sort(key=lambda r: r[1], reverse=True)
    symbols = [s for s, _ in vol_rows]
    if args.top_n:
        symbols = symbols[:args.top_n]
    _log(f"{len(symbols)} Kandidaten-Symbole x {len(timeframes)} Timeframes = "
         f"{len(symbols)*len(timeframes)} Kombinationen.")

    end_date = date.today().strftime('%Y-%m-%d')
    start_date = (date.today() - timedelta(weeks=args.lookback_weeks)).strftime('%Y-%m-%d')
    # Generischer Default fuer Kandidaten (die haben ja noch keine eigene
    # Config) -- Median der 13 Referenz-Strategien, statt frei erfunden.
    generic_avg_period = int(round(ref_df['avg_period'].median())) if 'avg_period' in ref_df else 20
    generic_env_pct = float(ref_df['env1'].median()) if 'env1' in ref_df else 0.02
    _log(f"Generische Kandidaten-Parameter: average_period={generic_avg_period}, envelope1={generic_env_pct*100:.2f}%")

    def _process_one(symbol_tf):
        symbol, tf = symbol_tf
        try:
            df = load_data(symbol, tf, start_date, end_date)
            stats = compute_fit_stats(df, generic_avg_period, generic_env_pct)
            if stats:
                stats.update({'symbol': symbol, 'timeframe': tf})
                return stats
        except Exception as e:
            _log(f"  {symbol} ({tf}): Fehler {e}")
        return None

    tasks = [(sym, tf) for sym in symbols for tf in timeframes]
    rows = []
    t0 = time.time()
    done = 0
    # I/O-gebunden (Netzwerk-Fetches) -- parallele Threads bringen hier einen
    # echten Speedup, da load_data() pro Aufruf ein eigenes Exchange-Objekt
    # anlegt (siehe backtester.py) und verschiedene Symbol/Timeframe-Caches
    # nie denselben Pfad treffen, also nichts kollidiert.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_process_one, t): t for t in tasks}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                rows.append(result)
            done += 1
            if done % 50 == 0 or done == len(tasks):
                elapsed = time.time() - t0
                _log(f"[{done}/{len(tasks)}] Kombinationen verarbeitet "
                     f"({elapsed:.0f}s, {elapsed/done:.2f}s/Kombo, {args.workers} Worker)")

    if not rows:
        _log("Keine Kandidaten-Daten berechnet.")
        return
    cand_df = pd.DataFrame(rows)
    ranked = rank_candidates(ref_df, cand_df)

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    ranked.to_csv(CSV_PATH, index=False)

    print(f"\n{'='*95}")
    print(f"  Top 30 nach Aehnlichkeit zum Referenz-Profil (kleinste fit_distance zuerst)")
    print(f"{'='*95}")
    print(f"  {'Symbol':<14}{'TF':<6}{'Range%':<9}{'Trend%':<9}{'STrend%':<9}{'ATR%':<8}{'Touch/Wo':<10}{'Distanz':<8}")
    for _, r in ranked.head(30).iterrows():
        print(f"  {r['symbol']:<14}{r['timeframe']:<6}{r['range_pct']:<9}{r['trend_pct']:<9}"
              f"{r['strong_trend_pct']:<9}{r['atr_pct']:<8}{r['touches_per_week']:<10}{r['fit_distance']:.3f}")
    print(f"{'='*95}")
    print(f"  Referenz-Median (13 aktive Strategien): {ref_df[METRIC_COLS].median().to_dict()}")
    print(f"  Volle Ergebnisliste: {CSV_PATH}")


if __name__ == '__main__':
    main()
