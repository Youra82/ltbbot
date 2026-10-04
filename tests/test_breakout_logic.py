# tests/test_breakout_logic.py
"""Band-Durchbruch-Modus (2026-10-04): geteilte Logik + Backtest-Grundverhalten, rein lokal."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'src'))
from ltbbot.strategy.breakout_logic import (breakout_signal, breakout_levels, position_size, regime_allows,
                                            btc_regime_from_daily)
from ltbbot.analysis.breakout_backtester import simulate_breakout_trades

P = {'strategy': {'mode': 'breakout', 'average_type': 'SMA', 'average_period': 20, 'atr_period': 14, 'band_atr': 3.0,
                  'max_hold_candles': 5, 'use_btc_filter': False, 'btc_sma_days': 200},
     'risk': {'sl_atr': 3.0, 'tp_r': 4.0, 'risk_per_entry_pct': 2.0, 'leverage': 5},
     'behavior': {'use_longs': True, 'use_shorts': False}}


def test_signal_only_above_band_and_longs_only():
    assert breakout_signal(110, 100, 3, P) == 'long'       # 110 > 100 + 9
    assert breakout_signal(108, 100, 3, P) is None
    assert breakout_signal(80, 100, 3, P) is None           # Shorts aus


def test_levels_and_sizing_min_notional():
    sl, tp = breakout_levels('long', 100.0, 2.0, P)
    assert sl == 94.0 and tp == 124.0                       # SL 3 ATR, TP 4R
    amt, margin = position_size(100.0, 100.0, 94.0, P)      # Risiko 2 USDT / 6 % = 33.3 USDT Notional
    assert abs(amt * 100 - 100 / 3) < 1e-6 and abs(margin - 100 / 3 / 5) < 1e-6
    assert position_size(5.0, 100.0, 94.0, P) is None       # 0.1/0.06 = 1.7 USDT < 5 USDT Mindestorder


def test_btc_regime_uses_previous_day():
    idx = pd.date_range('2025-01-01', periods=210, freq='D', tz='UTC')
    close = pd.Series(np.r_[np.full(205, 100.0), [200.0] * 5], index=idx)
    reg = btc_regime_from_daily(pd.DataFrame({'close': close}), 200)
    first_up = idx[205]
    assert not reg[first_up.normalize()]                    # Vortag noch unter/auf SMA
    assert bool(reg[idx[206].normalize()])
    p = {**P, 'strategy': {**P['strategy'], 'use_btc_filter': True}}
    assert regime_allows('long', idx[207], reg, p) and not regime_allows('long', idx[100], reg, p)


def _candles(closes, spread=0.5):
    idx = pd.date_range('2025-01-01', periods=len(closes), freq='4h', tz='UTC')
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({'open': o, 'high': np.maximum(o, c) + spread, 'low': np.minimum(o, c) - spread,
                         'close': c}, index=idx)


def test_backtest_enters_next_open_and_time_stop():
    closes = [100.0] * 40 + [130.0] + [131.0] * 20           # Ausbruch in Kerze 40
    tr, _, cap = simulate_breakout_trades(_candles(closes), P, 1000.0)
    assert len(tr) >= 1
    t = tr[0]
    assert t['entry_time'] == _candles(closes).index[41]     # Einstieg zum Open der Folgekerze
    assert t['exit_reason'] == 'TIME'                        # weder SL noch TP -> Zeitstopp nach 5 Kerzen
