# tests/test_backtester_no_lookahead.py
"""Rein lokale Tests (synthetische Kerzen, keine Bitget-Verbindung) gegen den
Band-Lookahead, der bis 2026-09-27 in backtester.py/portfolio_simulator.py
steckte: Baender/TP/Regime kamen aus der LAUFENDEN Kerze (inkl. deren Close),
live dagegen aus der letzten abgeschlossenen (drop_incomplete_last_candle)."""
import os
import sys
import logging

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.analysis.backtester import run_envelope_backtest
from ltbbot.analysis.portfolio_simulator import run_portfolio_simulation
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals, simulate_entry_fill

logging.disable(logging.CRITICAL)

PARAMS = {
    'market': {'symbol': 'TEST/USDT:USDT', 'timeframe': '1h'},
    'strategy': {'average_type': 'SMA', 'average_period': 5, 'envelopes': [0.01, 0.02, 0.03],
                 'trigger_price_delta_pct': 0.05, 'disable_strong_trend_block': True},
    'risk': {'leverage': 5, 'risk_per_entry_pct': 1.0, 'sl_to_env1_ratio': 0.5, 'margin_mode': 'isolated'},
    'behavior': {'use_longs': True, 'use_shorts': True},
}


def _random_walk(n=1500, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.normal(0, 0.008, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    idx = pd.date_range('2025-01-01', periods=n, freq='1h', tz='UTC')
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close, 'volume': 1.0}, index=idx)


def test_entries_use_bands_of_previous_closed_candle():
    data = _random_walk()
    df, _ = calculate_indicators_and_signals(data.copy(), PARAMS)
    res = run_envelope_backtest(data, PARAMS, 1000, show_progress=False)
    assert res['trades'], "Synthetische Daten sollten Trades erzeugen"
    delta = PARAMS['strategy']['trigger_price_delta_pct'] / 100.0
    for t in res['trades']:
        prev = df.iloc[df.index.get_loc(t['entry_time']) - 1]
        k = t['band']
        if t['side'] == 'long':
            expected = prev[f'band_low_{k}'] * (1 - delta)
        else:
            expected = prev[f'band_high_{k}'] * (1 + delta)
        assert np.isclose(t['entry_price'], expected), (t, expected)


def test_entry_decision_ignores_close_of_running_candle():
    """Ob/zu welchem Preis in Kerze i eingestiegen wird, darf nicht vom Close von
    Kerze i abhaengen (nur vom Preisverlauf open/high/low bis zum Fill)."""
    data = _random_walk()
    base = run_envelope_backtest(data, PARAMS, 1000, show_progress=False)
    first = base['trades'][0]
    ts = first['entry_time']
    mutated = data.copy()
    # Close der Entry-Kerze verschieben, high/low unveraendert (Close bleibt im Range)
    lo, hi = mutated.at[ts, 'low'], mutated.at[ts, 'high']
    mutated.at[ts, 'close'] = hi if mutated.at[ts, 'close'] < (lo + hi) / 2 else lo
    res = run_envelope_backtest(mutated, PARAMS, 1000, show_progress=False)
    same = [t for t in res['trades'] if t['entry_time'] == ts and t['band'] == first['band'] and t['side'] == first['side']]
    assert same and np.isclose(same[0]['entry_price'], first['entry_price'])


def test_portfolio_simulator_matches_backtester_for_single_strategy():
    data = _random_walk()
    bt = run_envelope_backtest(data, PARAMS, 1000, show_progress=False)
    ps = run_portfolio_simulation(1000, {'s': {'symbol': 'TEST/USDT:USDT', 'timeframe': '1h', 'data': data, 'params': PARAMS}},
                                  str(data.index[0].date()), str(data.index[-1].date()))
    assert ps['trade_count'] == bt['trades_count']
    assert np.isclose(ps['trades_df']['pnl_usd'].sum(), sum(t['pnl'] for t in bt['trades']), atol=0.05)


def test_simulate_entry_fill_rules():
    # Long, Preis oeffnet ueber Trigger, faellt hin -> Fill am Trigger, SL beruehrt -> Stop
    assert simulate_entry_fill('long', 101, 101.5, 98.9, 100.5, 100, 99) == (100, True)
    # Long, oeffnet schon jenseits SL -> live uebersprungen
    assert simulate_entry_fill('long', 98.5, 101, 98, 100.5, 100, 99) == (None, False)
    # Long, oeffnet zwischen SL und Trigger, laeuft zurueck -> Fill, Close ueber SL -> kein Stop
    assert simulate_entry_fill('long', 99.5, 100.2, 98.8, 99.8, 100, 99) == (100, False)
    # Long, laeuft nie zurueck zum Trigger -> kein Fill
    assert simulate_entry_fill('long', 99.5, 99.9, 99.1, 99.6, 100, 99) == (None, False)
    # Short gespiegelt
    assert simulate_entry_fill('short', 99, 101.1, 98.5, 99.5, 100, 101) == (100, True)
    assert simulate_entry_fill('short', 101.5, 102, 99, 99.5, 100, 101) == (None, False)
