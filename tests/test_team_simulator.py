"""Team-Simulator fuer den Portfolio-Optimizer (2026-11-07): Adapter simulate_team (gleiches Ergebnis-Format wie
run_portfolio_simulation) und Mark-Cache (komprimiert, nur Noetigstes, kein alter Ballast)."""
import copy
import os
import sys
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))

import ltbbot.analysis.backtester as bt
import ltbbot.analysis.portfolio_live_sim as pls
from test_rt_mode import rt_params


def _write_mark(path, start, minutes):
    idx = pd.date_range(start, periods=minutes, freq='1min', tz='UTC', name='timestamp')
    d = pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 0.0}, index=idx)
    bt._atomic_csv(d, path)


def test_prune_mark_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, 'PROJECT_ROOT', str(tmp_path))
    cache = tmp_path / 'data' / 'cache'
    cache.mkdir(parents=True)
    old_start = pd.Timestamp.now(tz='UTC').floor('min') - pd.Timedelta(days=bt.MARK_1M_KEEP_DAYS + 5)
    keep = bt._mark_cache_file('AAA/USDT:USDT', '1m')
    drop = bt._mark_cache_file('BBB/USDT:USDT', '1m')
    _write_mark(keep, old_start, 10 * 1440)                  # 5 Tage zu alt + 5 Tage im Fenster
    _write_mark(drop, old_start, 60)
    legacy = str(cache / 'AAA-USDT-USDT_1m_mark.csv')          # altes, unkomprimiertes Format
    open(legacy, 'w').write('timestamp,open\n')
    stale_tmp = keep + '.123.tmp'
    open(stale_tmp, 'w').write('x')
    os.utime(stale_tmp, (time.time() - 7200, time.time() - 7200))
    fresh_tmp = keep + '.456.tmp'                              # laufender Schreiber -> bleibt
    open(fresh_tmp, 'w').write('x')
    removed, trimmed = bt.prune_mark_cache({'AAA/USDT:USDT'})
    assert os.path.exists(keep) and not os.path.exists(drop) and not os.path.exists(legacy)
    assert not os.path.exists(stale_tmp) and os.path.exists(fresh_tmp)
    assert (removed, trimmed) == (3, 1)
    d = pd.read_csv(keep, index_col='timestamp', parse_dates=True)
    assert d.index.min() >= pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=bt.MARK_1M_KEEP_DAYS, minutes=1)
    assert keep.endswith('.csv.gz') and open(keep, 'rb').read(2) == b'\x1f\x8b'   # wirklich gzip


def _synthetic(start, days):
    """1m-Kurs: Seitwaerts mit taeglichem 12-%-Einbruch und Erholung -> Baender werden beruehrt, TP an der Mitte."""
    idx = pd.date_range(start, periods=days * 1440, freq='1min', tz='UTC')
    t = np.arange(len(idx))
    px = 100 * (1 - 0.12 * np.clip(np.sin(2 * np.pi * t / 1440), 0, None) ** 8)
    m = pd.DataFrame({'open': px, 'high': px * 1.001, 'low': px * 0.999, 'close': px}, index=idx)
    h = m.resample('1h').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    h['volume'] = 1.0
    return m, h


def test_simulate_team_matches_old_result_format(monkeypatch):
    m, h = _synthetic('2026-09-01', 40)
    p = copy.deepcopy(rt_params())
    p['strategy']['btc_trend_filter'] = False
    p['risk'].update(position_size_pct=15.0, leverage=6, stop_loss_pct=12.5)
    p['market'] = {'symbol': 'TST/USDT:USDT', 'timeframe': '1h'}
    monkeypatch.setitem(pls._MARK_MEM, 'TST/USDT:USDT', m[['open', 'high', 'low', 'close']])
    team = {'TST_1h': {'symbol': 'TST/USDT:USDT', 'timeframe': '1h', 'data': h, 'params': p}}
    res = pls.simulate_team(100.0, team, '2026-09-20', '2026-10-09', size_multiplier=None, max_open_strategies=None)
    for k in ('end_capital', 'total_pnl_pct', 'trade_count', 'win_rate', 'max_drawdown_pct', 'liquidation_date',
              'trades_df', 'equity_curve', 'pnl_per_strategy', 'trades_per_strategy'):
        assert k in res
    assert res['trade_count'] > 0
    tdf = res['trades_df']
    for c in ('exit_time', 'entry_time', 'symbol', 'timeframe', 'side', 'band', 'entry_price', 'exit_price',
              'amount_coins', 'pnl_usd', 'pnl_pct', 'reason', 'strategy_id', 'leverage'):
        assert c in tdf.columns
    assert (tdf['entry_time'] >= pd.Timestamp('2026-09-20', tz='UTC')).all()
    assert res['equity_curve'].index.name == 'timestamp' and 'equity' in res['equity_curve'].columns
    assert res['total_pnl_pct'] == pytest.approx((res['end_capital'] / 100.0 - 1) * 100)


def test_trim_keeps_requested_start():
    """Speichern kuerzt auf MARK_1M_KEEP_DAYS, aber nie den angefragten Zeitraum (aelteres Backtest-Fenster)."""
    now = pd.Timestamp.now(tz='UTC').floor('min')
    idx = pd.date_range(now - pd.Timedelta(days=bt.MARK_1M_KEEP_DAYS + 30), now, freq='1D', tz=None)
    d = pd.DataFrame({'close': 1.0}, index=pd.DatetimeIndex(idx).tz_convert('UTC') if idx.tz else idx.tz_localize('UTC'))
    assert d.index.min() < bt._trim_1m(d).index.min()                                  # ohne Anfrage: gekuerzt
    start = now - pd.Timedelta(days=bt.MARK_1M_KEEP_DAYS + 20)
    assert bt._trim_1m(d, keep_from=start).index.min() <= start + pd.Timedelta(days=1)  # Anfrage bleibt erhalten


def test_simulate_team_falls_back_without_marks(monkeypatch):
    """Fehlen Mark-Kerzen eines Team-Mitglieds, rechnet das ganze Team mit dem Kerzen-Simulator statt die
    Strategie stillschweigend wegzulassen."""
    m, h = _synthetic('2026-09-01', 40)
    p = copy.deepcopy(rt_params())
    p['strategy']['btc_trend_filter'] = False
    p['market'] = {'symbol': 'NOMARK/USDT:USDT', 'timeframe': '1h'}
    monkeypatch.setattr(bt, 'prefetch_mark_1m', lambda *a, **k: {})
    monkeypatch.setattr(bt, 'load_mark_data', lambda *a, **k: pd.DataFrame())
    called = {}
    import ltbbot.analysis.portfolio_simulator as ps
    monkeypatch.setattr(ps, 'run_portfolio_simulation', lambda *a, **k: called.setdefault('r', {'end_capital': 1.0}))
    team = {'NM_1h': {'symbol': 'NOMARK/USDT:USDT', 'timeframe': '1h', 'data': h, 'params': p}}
    logs = []
    res = pls.simulate_team(100.0, team, '2026-09-20', '2026-10-09', log=logs.append)
    assert called and res['simulator'].startswith('candle') and 'NOMARK/USDT:USDT' in logs[0]
