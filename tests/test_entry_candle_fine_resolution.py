# tests/test_entry_candle_fine_resolution.py
"""Ruecklauf-Fill in der Entry-Kerze per Fein-Kerzen aufloesen (2026-10-03, AVAX 2h live:
Fill 22:01, SL 22:16, Kerze schloss ueber dem SL -> Backtest hielt den Trade faelschlich)."""
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'src'))
from ltbbot.strategy.envelope_logic import simulate_entry_fill


def _bars(rows):
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'])


# Long, Open UNTER Trigger (Ruecklauf): Trigger 10.613, SL 10.574, Kerze schliesst darueber
O, H, L, C, TRIG, SL = 10.58, 10.70, 10.53, 10.62, 10.613, 10.574


def test_coarse_without_fine_keeps_close_rule():
    assert simulate_entry_fill('long', O, H, L, C, TRIG, SL) == (TRIG, False)


def test_fine_sl_after_fill_is_stop():
    fine = _bars([[10.58, 10.62, 10.58, 10.61],   # Fill (High >= Trigger), Close ueber SL
                  [10.61, 10.61, 10.547, 10.56],  # danach unter SL -> Stop
                  [10.56, 10.70, 10.55, 10.62]])
    assert simulate_entry_fill('long', O, H, L, C, TRIG, SL, fine_bars=fine) == (TRIG, True)


def test_fine_sl_only_before_fill_is_no_stop():
    fine = _bars([[10.58, 10.59, 10.53, 10.58],   # SL-Kontakt VOR dem Fill zaehlt nicht
                  [10.58, 10.62, 10.58, 10.61],   # Fill
                  [10.61, 10.70, 10.60, 10.62]])
    assert simulate_entry_fill('long', O, H, L, C, TRIG, SL, fine_bars=fine) == (TRIG, False)


def test_fine_callable_and_short_side():
    # Short, Open UEBER Trigger (Ruecklauf nach unten): Trigger 1.00, SL 1.02
    fine = _bars([[1.01, 1.03, 1.00, 1.005], [1.005, 1.025, 0.99, 1.0]])
    assert simulate_entry_fill('short', 1.01, 1.03, 0.99, 1.0, 1.00, 1.02, fine_bars=lambda: fine) == (1.00, True)


def test_falling_to_trigger_case_unchanged():
    # Long, Open UEBER Trigger: Kontakt mit SL nach Trigger ist zwingend -> Stop (ohne Fein-Daten)
    assert simulate_entry_fill('long', 10.65, 10.66, 10.56, 10.62, TRIG, SL) == (TRIG, True)
