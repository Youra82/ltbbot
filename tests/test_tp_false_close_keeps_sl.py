# tests/test_tp_false_close_keeps_sl.py
"""Rein lokal (Fake-Exchange, keine Bitget-Verbindung): meldet Bitget einen TP als
'closed', obwohl die Position noch offen ist, duerfen die Band-SLs NICHT storniert
werden (Live-Vorfall FIL 4h, 2026-10-02: offene Position ohne Stop)."""
import os
import sys
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.trade_manager import check_take_profit_trigger, read_tracker_file, update_tracker_file

log = logging.getLogger('test-tp-false-close')


class _Ccxt:
    id = 'bitget'
    has = {'fetchClosedOrders': True}

    def __init__(self, closed):
        self._closed = closed

    def fetchClosedOrders(self, symbol, limit=100, params=None):
        return self._closed


class FakeExchange:
    def __init__(self, position_open):
        self.exchange = _Ccxt([{'id': 'TP1', 'status': 'closed', 'stopPrice': 1.1}])
        self.position_open = position_open
        self.cancelled = []

    def fetch_open_positions(self, symbol):
        return [{'side': 'long', 'contracts': 5}] if self.position_open else []

    def fetch_plan_history_status(self, symbol, plan_type="normal_plan", since_ms=None):
        return {}

    def fetch_open_trigger_orders(self, symbol):
        # verwaiste separate Alt-SL ist an der Boerse noch offen
        return [{'id': 'SL1'}] if not self.position_open else []

    def cancel_trigger_order(self, order_id, symbol):
        self.cancelled.append(order_id)


def _tracker(tmp_path):
    path = str(tmp_path / 'tracker.json')
    update_tracker_file(path, {
        'take_profit_ids': ['TP1'],
        'committed_bands': {'long': [1], 'short': []},
        'band_sl_orders': {'long': {'1': 'SL1'}, 'short': {}},
        'band_sl_prices': {'long': {'1': 0.95}, 'short': {}},
    })
    return path


def test_false_tp_close_keeps_band_sl(tmp_path):
    path = _tracker(tmp_path)
    ex = FakeExchange(position_open=True)
    assert check_take_profit_trigger(ex, 'FIL/USDT:USDT', path, log) is False
    assert 'SL1' not in ex.cancelled
    t = read_tracker_file(path)
    assert t['band_sl_orders']['long'] == {'1': 'SL1'}
    assert t['committed_bands']['long'] == [1]


def test_real_tp_close_cancels_orphan_sl(tmp_path):
    path = _tracker(tmp_path)
    ex = FakeExchange(position_open=False)
    assert check_take_profit_trigger(ex, 'FIL/USDT:USDT', path, log) is True
    assert 'SL1' in ex.cancelled
    assert read_tracker_file(path)['band_sl_orders'] == {'long': {}, 'short': {}}
