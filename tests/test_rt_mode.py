# tests/test_rt_mode.py
"""RobotTraders-Modus (2026-10-05): Orders direkt am Band, keine Regime-Sperren, BTC-Trendfilter,
Sperre nach SL bis Close jenseits der Mitte, Kapitalanteil-Groesse mit 5-USDT-Anhebung.
Rein lokal (synthetische Kerzen, Fake-Exchange) -- keine echten Orders."""
import os
import sys
import logging

import pandas as pd
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.strategy.envelope_logic import (btc_trend_series, btc_trend_up_at, btc_side_allowed,
                                            fraction_band_amount, reentry_block_cleared, classify_regime,
                                            band_structure_ok, calculate_indicators_and_signals)
from ltbbot.analysis.backtester import run_envelope_backtest
import ltbbot.utils.trade_manager as tm

log = logging.getLogger('test-rt')


def rt_params(**strategy_over):
    strategy = {'average_type': 'DCM', 'average_period': 5, 'envelopes': [0.07, 0.11, 0.15],
                'trigger_price_delta_pct': 0.1, 'entry_mode': 'touch', 'regime_filter': False,
                'btc_trend_filter': True, 'reentry_after_sl': 'cross_average'}
    strategy.update(strategy_over)
    return {'market': {'symbol': 'TEST/USDT:USDT', 'timeframe': '4h'},
            'strategy': strategy,
            'risk': {'margin_mode': 'isolated', 'leverage': 1, 'stop_loss_pct': 25.0, 'sizing': 'fraction',
                     'position_size_pct': 30.0, 'min_notional_bump': True},
            'behavior': {'use_longs': True, 'use_shorts': False}}


def candles(rows, start='2026-01-01'):
    idx = pd.date_range(start, periods=len(rows), freq='4h', tz='UTC')
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'], index=idx).assign(volume=1.0)


FLAT = [100, 100.5, 99.5, 100]
BTC_UP = pd.Series([True], index=[pd.Timestamp('2020-01-01', tz='UTC')])
BTC_DOWN = pd.Series([False], index=[pd.Timestamp('2020-01-01', tz='UTC')])


# ---------------- geteilte Funktionen ----------------

def test_btc_trend_known_only_after_daily_close():
    idx = pd.date_range('2025-01-01', periods=201, freq='1D', tz='UTC')
    closes = [100.0] * 200 + [200.0]
    daily = pd.DataFrame({'close': closes}, index=idx)
    trend = btc_trend_series(daily, sma_period=200)
    last_day = idx[-1]
    assert btc_trend_up_at(trend, last_day + pd.Timedelta(hours=23)) is False  # Tageskerze laeuft noch
    assert btc_trend_up_at(trend, last_day + pd.Timedelta(days=1)) is True    # nach Tagesschluss bekannt
    assert btc_trend_up_at(trend, idx[0]) is None


def test_btc_side_allowed():
    p = rt_params()
    assert btc_side_allowed(p, 'long', True) and not btc_side_allowed(p, 'long', False)
    assert btc_side_allowed(p, 'short', False) and not btc_side_allowed(p, 'long', None)
    assert btc_side_allowed(rt_params(btc_trend_filter=False), 'long', False)


def test_fraction_amount_bumps_to_min_notional():
    p = rt_params()
    amt = fraction_band_amount(20.0, p, 2.0, 3)        # 20*30%/3 = 2 USDT < 5 -> angehoben
    assert amt * 2.0 == pytest.approx(5.05)
    amt = fraction_band_amount(1000.0, p, 2.0, 3)      # 100 USDT Notional je Band
    assert amt * 2.0 == pytest.approx(100.0)
    p['risk']['min_notional_bump'] = False
    assert fraction_band_amount(20.0, p, 2.0, 3) is None


def test_regime_filter_off_and_band_check():
    assert classify_regime(60.0, 100, 80, 120, 90, rt_params()['strategy']) == ("UNCERTAIN", True, "NEUTRAL")
    assert band_structure_ok(rt_params(), 0.30, 0.5, 0.25)  # Prozent-Baender werden nicht gegen ATR geprueft
    assert reentry_block_cleared('long', 101, 100) and not reentry_block_cleared('long', 100, 100)


# ---------------- Backtester ----------------

def _dip_series():
    rows = [FLAT] * 40
    rows.append([100, 100.2, 92.0, 95.0])   # Beruehrt Band 1 (DCM-Mitte 100 -> Band 93), Close UEBER dem Band
    rows.append([95, 101.0, 94.5, 100.0])   # Ruecklauf zur Mitte -> TP
    rows += [FLAT] * 5
    return candles(rows)


def test_touch_mode_fills_without_close_confirmation():
    res = run_envelope_backtest(_dip_series(), rt_params(), 1000, show_progress=False, btc_trend=BTC_UP)
    assert res['trades_count'] == 1
    t = res['trades'][0]
    assert t['band'] == 1 and t['exit_reason'] == 'TP' and t['pnl'] > 0


def test_classic_close_confirmation_skips_same_dip():
    p = rt_params(entry_mode='close_confirm')
    res = run_envelope_backtest(_dip_series(), p, 1000, show_progress=False, btc_trend=BTC_UP)
    assert res['trades_count'] == 0


def test_btc_filter_blocks_longs():
    res = run_envelope_backtest(_dip_series(), rt_params(), 1000, show_progress=False, btc_trend=BTC_DOWN)
    assert res['trades_count'] == 0


def _crash_then_touch(clear_before_touch):
    rows = [FLAT] * 40
    rows.append([100, 100.2, 60.0, 65.0])          # Crash: alle 3 Baender gefuellt und gestoppt
    rows += [[65, 66, 64, 64.9]] * 10              # Close unter der Mitte (65) -> Sperre bleibt
    if clear_before_touch:
        rows.append([65, 66, 64, 65.6])            # Close ueber der Mitte -> Sperre aufgehoben
    rows.append([65, 65.2, 60.0, 64.0])            # Beruehrt Band 1 (~60.45)
    rows += [[64, 66, 63.9, 65]] * 3
    return candles(rows)


def test_reentry_blocked_until_close_crosses_average():
    blocked = run_envelope_backtest(_crash_then_touch(False), rt_params(), 1000, show_progress=False, btc_trend=BTC_UP)
    assert blocked['trades_count'] == 3 and all(t['exit_reason'] == 'SL' for t in blocked['trades'])
    cleared = run_envelope_backtest(_crash_then_touch(True), rt_params(), 1000, show_progress=False, btc_trend=BTC_UP)
    assert cleared['trades_count'] > 3
    no_block = run_envelope_backtest(_crash_then_touch(False), rt_params(reentry_after_sl=None), 1000,
                                     show_progress=False, btc_trend=BTC_UP)
    assert no_block['trades_count'] > 3


# ---------------- Live: place_entry_orders (Fake-Exchange) ----------------

class FakeExchange:
    def __init__(self, price):
        self.price = price
        self.orders = []
        self.account = {'name': 'test'}

    def fetch_min_amount_tradable(self, symbol):
        return 0.0001

    def fetch_ticker(self, symbol):
        return {'last': self.price}

    def place_trigger_limit_order(self, symbol, side, amount, trigger_price, price, stop_loss_price=None):
        self.orders.append({'side': side, 'amount': amount, 'trigger': trigger_price, 'price': price,
                            'sl': stop_loss_price})
        return {'id': f'o{len(self.orders)}'}


@pytest.fixture
def no_telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(tm, 'send_message', lambda *a, **k: sent.append(a))
    monkeypatch.setattr(tm, '_send_ltbbot_chart', lambda *a, **k: sent.append(a))
    return sent


def _live_inputs():
    df = candles([FLAT] * 60)
    df_ind, band_prices = calculate_indicators_and_signals(df, rt_params())
    return df_ind, band_prices


def test_live_touch_mode_places_all_long_bands(tmp_path, no_telegram):
    df_ind, band_prices = _live_inputs()
    ex = FakeExchange(price=100.0)
    tracker = str(tmp_path / 't.json')
    tm.place_entry_orders(ex, band_prices, rt_params(), 20.0, tracker, {}, log, df=df_ind)
    assert [o['side'] for o in ex.orders] == ['buy', 'buy', 'buy']       # keine Close-Bestaetigung, keine Shorts
    for o, env in zip(ex.orders, [0.07, 0.11, 0.15]):
        band = band_prices['average'] * (1 - env)
        assert o['price'] == pytest.approx(band)
        assert o['sl'] == pytest.approx(band * 0.75)                      # 25 % Not-SL je Band
        assert o['amount'] * band == pytest.approx(5.05)                  # 20 USDT -> Anhebung auf 5 USDT
    assert no_telegram == []                                              # kein Spam bei jedem Neu-Platzieren


def test_live_blocked_side_places_nothing(tmp_path, no_telegram):
    df_ind, band_prices = _live_inputs()
    ex = FakeExchange(price=100.0)
    tm.place_entry_orders(ex, band_prices, rt_params(), 20.0, str(tmp_path / 't.json'), {}, log,
                          df=df_ind, blocked_sides={'long'})
    assert ex.orders == []


def test_live_sl_block_set_and_cleared(tmp_path):
    tracker = str(tmp_path / 't.json')
    tm.update_tracker_file(tracker, {'committed_bands': {'long': [], 'short': []}})
    before = {'long': [1, 2], 'short': []}
    sl_before = {'long': {'1': 69.75, '2': 66.75}, 'short': {}}
    # Kurs nahe SL -> SL-Ausstieg -> Sperre
    tm.update_sl_reentry_block(tracker, before, sl_before, pd.Timestamp('2026-01-07 16:00', tz='UTC'), 68.0, log, 'X')
    df = candles([[65, 66, 64, 64.9]] * 6, start='2026-01-07')   # letzte Kerze 20:00 = nach der Sperre
    df = df.assign(average=65.0)
    df.iloc[-2, df.columns.get_loc('close')] = 65.6               # Kerze 16:00 = Erkennungskerze zaehlt nicht
    assert tm.blocked_sides_after_sl(tracker, df.iloc[:-1], log, 'X') == {'long'}
    df.iloc[-1, df.columns.get_loc('close')] = 65.6                       # spaetere Kerze schliesst ueber Mitte
    assert tm.blocked_sides_after_sl(tracker, df, log, 'X') == set()


def test_live_tp_exit_does_not_block(tmp_path):
    tracker = str(tmp_path / 't.json')
    tm.update_tracker_file(tracker, {'committed_bands': {'long': [], 'short': []}})
    tm.update_sl_reentry_block(tracker, {'long': [1], 'short': []}, {'long': {'1': 69.75}, 'short': {}},
                               pd.Timestamp('2026-01-07', tz='UTC'), 99.0, log, 'X')   # Kurs an der Mitte = TP
    assert not (tm.read_tracker_file(tracker).get('sl_reentry_block') or {}).get('long')


def test_fraction_amount_rounds_up_to_contract_step():
    p = rt_params()
    amt = fraction_band_amount(20.0, p, 14.0, 3, amount_step=1.0)   # LINK: 5 USDT = 0.36 Coins -> 1 Coin
    assert amt == 1.0
    p['market']['amount_step'] = 0.1                                # Backtest: Schrittweite aus der Config
    assert fraction_band_amount(20.0, p, 2.0, 3) == pytest.approx(2.6)


def test_filled_and_stopped_bands_keep_margin_within_candle():
    # 12 USDT Konto, Hebel 1, je Band 5.05 USDT (Anhebung) -> nur 2 Baender passen in die Marge.
    # Crash-Kerze fuellt und stoppt alle 3 Baender: live lehnt Bitget das dritte mangels Marge ab.
    rows = [FLAT] * 40 + [[100, 100.2, 60.0, 65.0]] + [[65, 66, 64, 64.9]] * 3
    res = run_envelope_backtest(candles(rows), rt_params(), 12.0, show_progress=False, btc_trend=BTC_UP)
    assert res['trades_count'] == 2 and all(t['exit_reason'] == 'SL' for t in res['trades'])


# ---------------- Short-Seite mit eigenen Parametern ----------------

def rt_params_short(**kw):
    p = rt_params(**kw)
    p['strategy']['short'] = {'average_type': 'EMA', 'average_period': 20, 'envelopes': [0.10, 0.14, 0.18]}
    p['risk']['short_stop_loss_pct'] = 30.0
    p['behavior']['use_shorts'] = True
    return p


def _spike_series():
    rows = [FLAT] * 60
    rows.append([100, 111.0, 99.8, 104.0])    # Spitze ueber Short-Band 1 (EMA20 ~100 -> 111.1 /1.1? -> Band 1 = 100/0.9 = 111.1)
    rows.append([104, 104.5, 99.0, 100.0])    # Ruecklauf zur Short-Mitte -> TP
    rows += [FLAT] * 5
    rows[60][1] = 111.5
    return candles(rows)


def test_short_uses_own_bands_and_only_in_btc_downtrend():
    p = rt_params_short()
    up = run_envelope_backtest(_spike_series(), p, 1000, show_progress=False, btc_trend=BTC_UP)
    assert up['trades_count'] == 0                                     # BTC > SMA200: kein Short
    down = run_envelope_backtest(_spike_series(), p, 1000, show_progress=False, btc_trend=BTC_DOWN)
    assert down['trades_count'] == 1
    t = down['trades'][0]
    assert t['side'] == 'short' and t['band'] == 1 and t['exit_reason'] == 'TP' and t['pnl'] > 0
    assert t['entry_price'] == pytest.approx(100 / 0.9 * 1.001, rel=1e-3)  # Short-Band 1: EMA-Mitte / (1-10 %)


def test_short_sl_uses_short_stop_pct_and_bands_from_short_mid():
    df = candles([FLAT] * 60)
    p = rt_params_short()
    df_ind, bp = calculate_indicators_and_signals(df, p)
    assert bp['short'][0] == pytest.approx(bp['average_short'] / 0.9)
    assert bp['long'][0] == pytest.approx(bp['average'] * 0.93)
    from ltbbot.strategy.envelope_logic import compute_band_sl_price
    assert compute_band_sl_price('short', 110.0, 0, p, 'UNCERTAIN') == pytest.approx(110.0 * 1.30)
    assert compute_band_sl_price('long', 93.0, 0, p, 'UNCERTAIN') == pytest.approx(93.0 * 0.75)


def test_live_places_short_orders_only_when_btc_down(tmp_path, no_telegram):
    df = candles([FLAT] * 60)
    p = rt_params_short()
    df_ind, bp = calculate_indicators_and_signals(df, p)
    ex = FakeExchange(price=100.0)
    tm.place_entry_orders(ex, bp, p, 20.0, str(tmp_path / 't.json'), {}, log, df=df_ind, blocked_sides={'long'})
    assert [o['side'] for o in ex.orders] == ['sell', 'sell', 'sell']
    assert ex.orders[0]['price'] == pytest.approx(bp['average_short'] / 0.9)
    assert ex.orders[0]['sl'] == pytest.approx(ex.orders[0]['price'] * 1.30)


# ---------------- Short-Hebel A (Regime-Ausstieg), B (SMA50), C (Groesse) ----------------

def _btc_frame(up, below50):
    return pd.DataFrame({'up': [up], 'below50': [below50]}, index=[pd.Timestamp('2020-01-01', tz='UTC')])


def test_short_sma50_filter_B():
    p = rt_params_short()
    p['strategy']['short']['btc_filter'] = 'sma200_sma50'
    assert not btc_side_allowed(p, 'short', False, False)   # BTC unter SMA200, aber ueber SMA50 (Rallye)
    assert btc_side_allowed(p, 'short', False, True)
    res = run_envelope_backtest(_spike_series(), p, 1000, show_progress=False, btc_trend=_btc_frame(False, False))
    assert res['trades_count'] == 0
    res = run_envelope_backtest(_spike_series(), p, 1000, show_progress=False, btc_trend=_btc_frame(False, True))
    assert res['trades_count'] == 1


def test_short_regime_exit_A():
    p = rt_params_short()
    p['strategy']['short']['regime_exit'] = True
    rows = [FLAT] * 60 + [[100, 111.5, 99.8, 104.0]] + [[104, 105, 103.5, 104.5]] * 6
    df = candles(rows)
    flip = df.index[63]                                       # BTC dreht ab Kerze 63 nach oben
    trend = pd.DataFrame({'up': [False, True], 'below50': [True, False]}, index=[pd.Timestamp('2020-01-01', tz='UTC'), flip])
    res = run_envelope_backtest(df, p, 1000, show_progress=False, btc_trend=trend)
    assert res['trades_count'] == 1
    t = res['trades'][0]
    assert t['side'] == 'short' and t['exit_reason'] == 'REGIME' and t['exit_time'] == flip


def test_short_position_size_C():
    p = rt_params_short()
    p['risk']['short_position_size_pct'] = 15.0
    assert fraction_band_amount(1000.0, p, 10.0, 3, side='short') * 10.0 == pytest.approx(50.0)
    assert fraction_band_amount(1000.0, p, 10.0, 3, side='long') * 10.0 == pytest.approx(100.0)


def test_concurrency_limit_helpers():
    from ltbbot.strategy.envelope_logic import max_concurrent_positions, concurrency_allows_new
    assert max_concurrent_positions({'live_trading_settings': {'max_concurrent_positions': 10}}) == 10
    assert max_concurrent_positions({'live_trading_settings': {}}) is None
    assert concurrency_allows_new(9, 10) and not concurrency_allows_new(10, 10) and concurrency_allows_new(50, None)


def test_portfolio_sim_respects_max_open_strategies():
    from ltbbot.analysis.portfolio_simulator import run_portfolio_simulation
    sd = {}
    for k in range(3):
        df = _dip_series()
        sd[f's{k}'] = {'symbol': f'C{k}/USDT:USDT', 'timeframe': '4h', 'params': rt_params(), 'data': df}
    import ltbbot.analysis.backtester as bt
    orig = bt.load_btc_trend
    import ltbbot.analysis.portfolio_simulator as ps
    ps.load_btc_trend = lambda *a, **k: BTC_UP
    try:
        start, end = str(_dip_series().index[0].date()), str(_dip_series().index[-1].date())
        two = run_portfolio_simulation(1000, sd, start, end, max_open_strategies=2)
        none = run_portfolio_simulation(1000, sd, start, end, max_open_strategies=None)
    finally:
        ps.load_btc_trend = orig
    assert two['trade_count'] == 2 and none['trade_count'] == 3


def test_low_balance_notice_only_on_state_change(tmp_path):
    """Telegram-Hinweis 'Guthaben gering' nur einmal kontoweit beim Eintritt/Verlassen, nicht je Symbol/Zyklus."""
    from ltbbot.utils.trade_manager import low_balance_transition
    m = str(tmp_path / '_low_balance.flag')
    assert low_balance_transition(5.0, marker_path=m) is None
    assert low_balance_transition(0.01, marker_path=m) == 'entered'
    for _ in range(20):  # weitere Symbole / Zyklen: still
        assert low_balance_transition(0.01, marker_path=m) is None
    assert low_balance_transition(2.0, marker_path=m) is None  # Hysterese
    assert low_balance_transition(8.0, marker_path=m) == 'recovered'
    assert low_balance_transition(8.0, marker_path=m) is None
    assert low_balance_transition(0.5, marker_path=m) == 'entered'


def test_tp_already_reached_matches_backtest_gap():
    """LPT 4h 2026-10-08: Mitte (TP) fiel unter den Kurs -> live muss schliessen (Backtest: Ausstieg zum Open)."""
    from ltbbot.strategy.envelope_logic import tp_already_reached
    assert tp_already_reached('long', 1.688, 1.673)          # Kurs ueber Long-Mitte -> TP erreicht
    assert not tp_already_reached('long', 1.660, 1.673)
    assert tp_already_reached('short', 0.95, 1.00)            # Kurs unter Short-Mitte
    assert not tp_already_reached('short', 1.05, 1.00)
    assert not tp_already_reached('long', None, 1.0) and not tp_already_reached('long', 1.0, float('nan'))


def test_close_position_market_closes_long_with_sell(tmp_path, no_telegram):
    class Ex:
        def __init__(self): self.calls = []
        def cancel_all_orders_for_symbol(self, s): self.calls.append(('cancel', s))
        def fetch_open_positions(self, s): return [{'side': 'long', 'contracts': '2.8'}]
        def place_market_order(self, s, side, amount, reduce=False): self.calls.append(('market', side, amount, reduce))
    ex = Ex(); tracker = str(tmp_path / 't.json')
    tm.update_tracker_file(tracker, {'committed_bands': {'long': [1], 'short': []}, 'take_profit_ids': ['x']})
    assert tm._close_position_market(ex, 'LPT/USDT:USDT', tracker, {}, log, 'TP-Ausstieg', 'msg')
    assert ex.calls == [('cancel', 'LPT/USDT:USDT'), ('market', 'sell', 2.8, True)]
    t = tm.read_tracker_file(tracker)
    assert t['committed_bands'] == {'long': [], 'short': []} and t['take_profit_ids'] == []
    assert len(no_telegram) == 1
