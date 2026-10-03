# tests/test_attached_band_sl.py
"""Rein lokal (Fake-Exchange): Band-SL haengt an der Entry-Order (stopLossTriggerPrice)
statt als separate Trigger-Order vor dem Fill. Live-Anlass MOVR 2h, 2026-10-02/03: die
separate SL zuendete ohne Position ins Leere ('fail_execute'), die Entry blieb aktiv.
Bitget-Verhalten live verifiziert (DOGE, ccxt 4.3.5): beim Fill entsteht je Entry ein
'loss_plan' (Kategorie profit_loss) mit genau der gefuellten Menge."""
import os
import sys
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.trade_manager import (link_attached_band_sls, check_stop_loss_trigger,
                                        read_tracker_file, update_tracker_file)

log = logging.getLogger('test-attached-sl')


def _loss_plan(oid, trig, side='long', size=60):
    return {'id': oid, 'stopPrice': trig, 'amount': size,
            'info': {'planType': 'loss_plan', 'posSide': side, 'tradeSide': 'close', 'triggerPrice': str(trig)}}


class FakeExchange:
    def __init__(self, tpsl=(), history=None, closed=()):
        self.tpsl = list(tpsl)
        self.history = history or {}
        self.exchange = type('X', (), {'id': 'bitget', 'has': {'fetchClosedOrders': True},
                                       'fetchClosedOrders': lambda s, *a, **k: list(closed)})()

    def fetch_position_tpsl_orders(self, symbol):
        return self.tpsl

    def fetch_position_tpsl_history(self, symbol, since_ms=None):
        return self.history

    def fetch_open_trigger_orders(self, symbol):
        return []


def _tracker(tmp_path, **kw):
    path = str(tmp_path / 'tracker.json')
    data = {'committed_bands': {'long': [1, 2], 'short': []},
            'band_sl_orders': {'long': {}, 'short': {}},
            'band_sl_prices': {'long': {'1': 0.08983, '2': 0.08891}, 'short': {}}}
    data.update(kw)
    update_tracker_file(path, data)
    return path


def test_attached_sls_linked_by_price(tmp_path):
    path = _tracker(tmp_path)
    ex = FakeExchange(tpsl=[_loss_plan('A', 0.08891), _loss_plan('B', 0.08983)])
    link_attached_band_sls(ex, 'DOGE/USDT:USDT', path, log)
    assert read_tracker_file(path)['band_sl_orders']['long'] == {'1': 'B', '2': 'A'}


def test_wrong_side_or_far_price_not_linked(tmp_path):
    path = _tracker(tmp_path)
    ex = FakeExchange(tpsl=[_loss_plan('S', 0.08983, side='short'), _loss_plan('F', 0.0850)])
    link_attached_band_sls(ex, 'DOGE/USDT:USDT', path, log)
    assert read_tracker_file(path)['band_sl_orders']['long'] == {}


def test_executed_attached_sl_releases_band(tmp_path):
    path = _tracker(tmp_path, band_sl_orders={'long': {'1': 'B', '2': 'A'}, 'short': {}})
    ex = FakeExchange(tpsl=[_loss_plan('A', 0.08891)], history={'B': 'executed'})
    assert check_stop_loss_trigger(ex, 'DOGE/USDT:USDT', path, log, current_candle_ts='T') is True
    t = read_tracker_file(path)
    assert t['committed_bands']['long'] == [2]
    assert t['band_sl_orders']['long'] == {'2': 'A'}
    assert t['sl_fired_candle_ts']['long']['1'] == 'T'


def test_open_attached_sl_keeps_band(tmp_path):
    path = _tracker(tmp_path, band_sl_orders={'long': {'1': 'B'}, 'short': {}})
    ex = FakeExchange(tpsl=[_loss_plan('B', 0.08983)])
    assert check_stop_loss_trigger(ex, 'DOGE/USDT:USDT', path, log) is False
    assert read_tracker_file(path)['committed_bands']['long'] == [1, 2]


def test_place_trigger_limit_order_attaches_sl():
    from ltbbot.utils.exchange import Exchange
    sent = {}

    class Cx:
        def amount_to_precision(self, s, a): return str(a)
        def price_to_precision(self, s, p): return f"{p:.5f}"
        def create_order(self, *a, params=None):
            sent.update(params); return {'id': 'E1'}

    ex = Exchange.__new__(Exchange)
    ex.exchange, ex.markets = Cx(), {'DOGE/USDT:USDT': {}}
    ex.amount_to_precision = lambda s, a: str(a)
    ex.price_to_precision = lambda s, p: f"{p:.5f}"
    ex.place_trigger_limit_order('DOGE/USDT:USDT', 'buy', 60, 0.0945, 0.0946, stop_loss_price=0.0930)
    assert sent['stopLossTriggerPrice'] == '0.09300' and sent['stopLossTriggerType'] == 'mark_price'
    assert sent['reduceOnly'] is False
