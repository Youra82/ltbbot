# tests/test_daily_live_vs_backtest_check.py
"""Rein lokale Tests fuer daily_live_vs_backtest_check.py -- keine Bitget-
Verbindung, kein Backtest-Lauf. Prueft nur die reine Logik: Config-Pfad-
Aufloesung, Faelligkeits-Check und Nachrichten-Zusammenbau."""
import os
import sys
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

import daily_live_vs_backtest_check as mod


def test_config_path_for_resolves_real_filename_pattern():
    path = mod._config_path_for('AAVE/USDT:USDT', '2h')
    assert path is not None
    assert path.endswith('config_AAVEUSDTUSDT_2h_envelope.json')
    assert os.path.exists(path)


def test_config_path_for_missing_combo_returns_none():
    path = mod._config_path_for('AAVE/USDT:USDT', '99h')
    assert path is None


def test_is_due_true_only_at_send_hour_and_not_yet_today(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LAST_RUN_FILE', str(tmp_path / 'last_run'))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 23, 8, 5)

    monkeypatch.setattr(mod, 'datetime', FixedDatetime)
    assert mod._is_due(8) is True
    assert mod._is_due(9) is False


def test_is_due_false_if_already_run_today(tmp_path, monkeypatch):
    last_run_file = tmp_path / 'last_run'
    last_run_file.write_text(datetime(2026, 9, 23, 8, 1).isoformat())
    monkeypatch.setattr(mod, 'LAST_RUN_FILE', str(last_run_file))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 23, 8, 10)

    monkeypatch.setattr(mod, 'datetime', FixedDatetime)
    assert mod._is_due(8) is False


def test_build_message_aggregates_totals_correctly():
    live = {
        'ADA': {'trades': 4, 'wins': 0, 'pnl_usd': -11.09, 'win_rate': 0.0},
        'AAVE': {'trades': 2, 'wins': 1, 'pnl_usd': 0.5, 'win_rate': 50.0},
    }
    backtest = {
        'ADA': {'trades': 9, 'win_rate': 56.0, 'pnl_usd': 3.99},
        'AAVE': {'trades': 2, 'win_rate': 50.0, 'pnl_usd': 0.59},
    }
    msg = mod.build_message(live, backtest, window_days=30)

    assert '6 Trades' in msg  # 4 + 2 Live-Trades gesamt
    assert '11 Trades' in msg  # 9 + 2 Backtest-Trades gesamt
    assert 'ADA' in msg
    assert 'letzte 30 Tage' in msg


def test_build_message_flags_live_only_symbol_without_crashing():
    """Ein Symbol mit Live-Trades aber ohne Backtest-Eintrag (z.B. Config
    fehlgeschlagen) darf die Nachricht nicht zum Absturz bringen."""
    live = {'XRP': {'trades': 3, 'wins': 1, 'pnl_usd': -0.2, 'win_rate': 33.3}}
    backtest = {}
    msg = mod.build_message(live, backtest, window_days=30)
    assert 'XRP' in msg
