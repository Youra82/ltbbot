# tests/test_cancel_orders_pending_sl.py
"""Rein lokal (Fake-Exchange): SLs nie gefuellter Baender werden mit ihrer Entry storniert,
SLs gefuellter Baender bleiben -- und bei existierender Position bleiben sicherheitshalber alle.
Live-Anlass FIL 4h, 2026-10-02: verwaiste 'Long schliessen'-Order nach Ende des Signals."""
import os
import sys
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.trade_manager import cancel_strategy_orders, update_tracker_file

log = logging.getLogger('test-cancel-pending-sl')


class FakeExchange:
    def __init__(self, triggers, position_sides=()):
        self._triggers = triggers
        self._pos = position_sides
        self.cancelled = []

    def fetch_open_orders(self, symbol):
        return []

    def fetch_open_trigger_orders(self, symbol):
        return [{'id': i, 'side': 'buy', 'amount': 1, 'stopPrice': 1.0} for i in self._triggers]

    def fetch_open_positions(self, symbol):
        return [{'side': s, 'contracts': 5} for s in self._pos]

    def cancel_trigger_order(self, order_id, symbol):
        self.cancelled.append(order_id)


def _tracker(tmp_path, committed_long):
    path = str(tmp_path / 'tracker.json')
    update_tracker_file(path, {
        'committed_bands': {'long': committed_long, 'short': []},
        'band_sl_orders': {'long': {'1': 'SL1', '2': 'SL2'}, 'short': {}},
        'pending_band_orders': {'long': {'2': 'E2'}, 'short': {}},
        'take_profit_ids': ['TP'],
    })
    return path


def test_flat_pending_band_sl_is_cancelled(tmp_path):
    path = _tracker(tmp_path, committed_long=[])
    ex = FakeExchange(['SL1', 'SL2', 'E2'])
    cancel_strategy_orders(ex, 'FIL/USDT:USDT', log, tracker_file_path=path)
    assert set(ex.cancelled) == {'SL1', 'SL2', 'E2'}


def test_committed_band_sl_is_kept(tmp_path):
    path = _tracker(tmp_path, committed_long=[1])
    ex = FakeExchange(['SL1', 'SL2', 'E2', 'TP'], position_sides=())
    cancel_strategy_orders(ex, 'FIL/USDT:USDT', log, tracker_file_path=path)
    assert 'SL1' not in ex.cancelled and 'TP' not in ex.cancelled
    assert 'SL2' in ex.cancelled and 'E2' in ex.cancelled


def test_position_open_keeps_all_band_sls(tmp_path):
    path = _tracker(tmp_path, committed_long=[])
    ex = FakeExchange(['SL1', 'SL2', 'E2'], position_sides=('long',))
    cancel_strategy_orders(ex, 'FIL/USDT:USDT', log, tracker_file_path=path)
    assert ex.cancelled == ['E2']
