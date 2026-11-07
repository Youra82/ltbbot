"""F2-Groesse (2026-11-07): 15 % Margin je Strategie, Hebel je Config aus dem SL, eigener Short-Hebel.
Live (trade_manager/exchange) und Live-Takt-Simulator muessen dieselben Mengen/Hebel rechnen."""
import copy
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))

from ltbbot.strategy.envelope_logic import (leverage_for_sl, side_leverage, fraction_band_amount,
                                            calculate_indicators_and_signals, calculate_position_margin)
from ltbbot.utils.exchange import Exchange
import ltbbot.utils.trade_manager as tm
from test_rt_mode import rt_params, candles, FLAT, FakeExchange, no_telegram  # noqa: F401 (Fixture)

log = logging.getLogger('test-f2')


@pytest.fixture(autouse=True)
def gate_neutral(monkeypatch):
    """Phasen-Tor auf x1 -- sonst rechnet der Livebot den Tagesfaktor des heutigen Stands ein."""
    monkeypatch.setattr(tm.phase_gate, 'current_multiplier', lambda *a, **k: (1.0, 'Test'))


def f2_params(sl=12.58):
    p = copy.deepcopy(rt_params())
    r = p['risk']
    r.update(position_size_pct=15.0, stop_loss_pct=sl, leverage=leverage_for_sl(sl, 8, 2),
             short_stop_loss_pct=30.0, short_position_size_pct=7.5, short_leverage=leverage_for_sl(30.0, 8, 1))
    return p


@pytest.mark.parametrize('sl, lev', [(10.0, 8), (10.01, 7), (12.58, 6), (24.0, 3), (39.68, 2), (60.0, 2)])
def test_leverage_for_sl_long(sl, lev):
    assert leverage_for_sl(sl, 8, 2) == lev
    assert 1 / lev > sl / 100 * 1.2 or lev == 2      # Liquidation (~1/Hebel) liegt hinter dem SL


def test_short_leverage_for_30pct_sl():
    assert leverage_for_sl(30.0, 8, 1) == 2


def test_side_leverage_uses_short_leverage():
    p = f2_params()
    assert side_leverage(p, 'long') == 6 and side_leverage(p, 'short') == 2
    del p['risk']['short_leverage']
    assert side_leverage(p, 'short') == 6            # ohne Eintrag wie bisher: ein Hebel fuer beide Seiten


def test_fraction_amount_uses_side_leverage():
    p = f2_params()
    # Long: 100 USDT * 15 % / 3 Baender = 5 USDT Marge * Hebel 6 = 30 USDT Notional
    assert fraction_band_amount(100.0, p, 2.0, 3, side='long') * 2.0 == pytest.approx(30.0)
    # Short: 100 * 7.5 % / 3 = 2.5 USDT Marge * Hebel 2 = 5 USDT Notional
    assert fraction_band_amount(100.0, p, 2.0, 3, side='short') * 2.0 == pytest.approx(5.0)


def test_live_orders_f2_size_and_margin(tmp_path, no_telegram):
    p = f2_params()
    df = candles([FLAT] * 60)
    df_ind, band_prices = calculate_indicators_and_signals(df, p)
    ex = FakeExchange(price=100.0)
    tm.place_entry_orders(ex, band_prices, p, 100.0, str(tmp_path / 't.json'), {}, log, df=df_ind)
    assert [o['side'] for o in ex.orders] == ['buy', 'buy', 'buy']
    for o in ex.orders:
        assert o['amount'] * o['price'] == pytest.approx(30.0)          # 5 USDT Marge x Hebel 6
        # gleiche Menge wie der Simulator (geteilte Funktion, gleiche Eingaben)
        assert o['amount'] == pytest.approx(fraction_band_amount(100.0, p, o['price'], 3, side='long'))
        assert calculate_position_margin(o['amount'], o['price'], side_leverage(p, 'long')) == pytest.approx(5.0)


def test_live_margin_check_counts_side_leverage(tmp_path, no_telegram):
    """Mit Hebel 6 passen alle 3 Baender (je 5 USDT Marge) in 15 USDT freies Kapital -- mit Hebel 2
    gerechnet (alter Fehler: Margin-Pruefung mit falschem Hebel) waeren es je 15 USDT."""
    p = f2_params()
    p['risk']['position_size_pct'] = 100.0                              # 3 x 33 % des freien Kapitals
    df_ind, band_prices = calculate_indicators_and_signals(candles([FLAT] * 60), p)
    ex = FakeExchange(price=100.0)
    tm.place_entry_orders(ex, band_prices, p, 15.0, str(tmp_path / 't.json'), {}, log, df=df_ind)
    assert len(ex.orders) == 3


class _FakeCcxt:
    def __init__(self):
        self.calls = []

    def set_leverage(self, leverage, symbol, params=None):
        self.calls.append((leverage, params.get('holdSide')))


def test_exchange_sets_one_leverage_for_both_sides():
    """Bitget setzt je Aufruf Long UND Short (holdSide wird ignoriert, live 2026-11-07) -> beide Aufrufe mit
    demselben Wert, sonst ueberschreibt der zweite den ersten."""
    ex = Exchange.__new__(Exchange)
    ex.markets = {'X/USDT:USDT': {}}
    ex.exchange = _FakeCcxt()
    ex.set_leverage('X/USDT:USDT', 6, margin_mode='isolated')
    assert ex.exchange.calls == [(6, 'long'), (6, 'short')]


def test_cycle_leverage_follows_allowed_side():
    p = f2_params()                                   # Long 6, Short 2
    assert tm.cycle_leverage(p, {'short'}) == 6       # BTC ueber SMA200: nur Long -> Long-Hebel
    assert tm.cycle_leverage(p, {'long'}) == 2        # BTC darunter: nur Short -> Short-Hebel
    assert tm.cycle_leverage(p, set()) == 2           # beide moeglich -> kleinerer (Liquidation hinter beiden SL)
    assert tm.cycle_leverage(p, {'long', 'short'}) == 2
    del p['risk']['short_leverage']
    assert tm.cycle_leverage(p, set()) == 6           # ohne eigenen Short-Hebel wie bisher


def test_apply_rt_sizing_migrates_old_configs():
    """apply_rt_sizing.py macht aus den Configs des alten Repo-Stands (60 %/Hebel 2) exakt die F2-Werte."""
    import json, subprocess
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from apply_rt_sizing import target_risk
    root = os.path.join(os.path.dirname(__file__), '..')
    rt = json.load(open(os.path.join(root, 'settings.json'), encoding='utf-8'))['rt_settings']
    f = 'src/ltbbot/strategy/configs/config_SANDUSDTUSDT_6h_envelope.json'
    old = json.loads(subprocess.run(['git', 'show', f'dbf8bee:{f}'], cwd=root, capture_output=True, text=True).stdout)
    assert old['risk']['position_size_pct'] == 60.0 and old['risk']['leverage'] == 2
    new = target_risk(old['risk'], rt)
    assert new == {**old['risk'], 'position_size_pct': rt['position_size_pct'], 'short_position_size_pct': rt['short']['position_size_pct'],
                   'leverage': 6, 'short_leverage': 2}
    assert target_risk(new, rt) == new                                  # wiederholbar
