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


def test_build_message_includes_flags_section():
    msg = mod.build_message({}, {}, window_days=7, flags=["⚠️ Testhinweis"])
    assert "Testhinweis" in msg
    assert "Automatische Hinweise" in msg


def test_build_message_without_flags_has_no_hint_section():
    msg = mod.build_message({}, {}, window_days=7, flags=[])
    assert "Automatische Hinweise" not in msg


# --- detect_outlier_trade_flag ---

def _trade(symbol, side, pnl_usd, ctime, utime=None):
    return {'symbol': symbol, 'side': side, 'pnl_usd': pnl_usd, 'ctime': ctime, 'utime': utime or ctime + 60000}


def test_outlier_trade_flag_reproduces_ada_incident():
    """Reproduziert den ADA-Vorfall vom 2026-09-03: ein Trade macht ~89% des
    Wochen-PnL-Volumens aus -- muss geflaggt werden."""
    trades = [
        _trade('ADA', 'short', -10.23, 1000, 2000),
        _trade('ADA', 'short', -0.14, 3000),
        _trade('AAVE', 'long', 0.68, 4000),
        _trade('XRP', 'short', -0.07, 5000),
    ]
    flag = mod.detect_outlier_trade_flag(trades)
    assert flag is not None
    assert 'ADA' in flag


def test_outlier_trade_flag_not_triggered_for_evenly_spread_losses():
    trades = [
        _trade('ADA', 'short', -0.3, 1000),
        _trade('AAVE', 'long', -0.25, 2000),
        _trade('XRP', 'short', -0.28, 3000),
        _trade('ARB', 'short', -0.31, 4000),
    ]
    assert mod.detect_outlier_trade_flag(trades) is None


def test_outlier_trade_flag_skipped_below_min_trades():
    trades = [_trade('ADA', 'short', -10.0, 1000), _trade('ADA', 'short', -0.1, 2000)]
    assert mod.detect_outlier_trade_flag(trades, min_trades=3) is None


# --- detect_cluster_flags ---

def test_cluster_flag_reproduces_arb_repeated_reentry():
    """Reproduziert das ARB/6h-Muster vom 2026-09-15/16: 7 Trades desselben
    Symbols innerhalb weniger Stunden. now_ms liegt kurz nach dem letzten
    Trade, damit der recent_only_hours-Filter nicht dazwischenfunkt."""
    base_ts = 1000 * 3600 * 1000
    trades = [_trade('ARB', 'short', -0.2, base_ts + i * 15 * 60 * 1000) for i in range(7)]
    flags = mod.detect_cluster_flags(trades, window_hours=3.0, min_count=3,
                                      now_ms=trades[-1]['ctime'] + 3600 * 1000)
    assert len(flags) == 1
    assert 'ARB' in flags[0]


def test_cluster_flag_not_triggered_for_spread_out_trades():
    base_ts = 1000 * 3600 * 1000
    trades = [_trade('ARB', 'short', -0.2, base_ts + i * 6 * 3600 * 1000) for i in range(5)]
    flags = mod.detect_cluster_flags(trades, window_hours=3.0, min_count=3,
                                      now_ms=trades[-1]['ctime'] + 3600 * 1000)
    assert flags == []


def test_cluster_flag_not_triggered_if_cluster_is_older_than_recent_only_hours():
    """Reproduziert live beobachtetes Verhalten 2026-09-24: ein Cluster vom
    17.09. wurde am 24.09. (7 Tage rollierendes Fenster) noch als 'aktuell'
    gemeldet, obwohl der Same-Candle-Fix laengst deployed war. Ein Cluster
    ausserhalb von recent_only_hours darf NICHT mehr geflaggt werden."""
    base_ts = 1000 * 3600 * 1000
    trades = [_trade('PEPE', 'short', -0.15, base_ts + i * 15 * 60 * 1000) for i in range(3)]
    now_ms = trades[-1]['ctime'] + 7 * 24 * 3600 * 1000  # 7 Tage nach dem Cluster
    flags = mod.detect_cluster_flags(trades, window_hours=3.0, min_count=3,
                                      recent_only_hours=48.0, now_ms=now_ms)
    assert flags == []


def test_cluster_flag_includes_date_when_triggered():
    base_ts = 1000 * 3600 * 1000
    trades = [_trade('ARB', 'short', -0.2, base_ts + i * 15 * 60 * 1000) for i in range(3)]
    flags = mod.detect_cluster_flags(trades, window_hours=3.0, min_count=3,
                                      now_ms=trades[-1]['ctime'] + 3600 * 1000)
    assert len(flags) == 1
    assert 'UTC' in flags[0]


# --- detect_trend_flags ---

def test_trend_flag_triggers_on_persistent_low_winrate():
    history = [
        {'date': '2026-09-20', 'trades': 5, 'win_rate': 10.0, 'pnl_usd': -1.0},
        {'date': '2026-09-21', 'trades': 6, 'win_rate': 15.0, 'pnl_usd': -0.5},
        {'date': '2026-09-22', 'trades': 4, 'win_rate': 0.0, 'pnl_usd': -2.0},
    ]
    flags = mod.detect_trend_flags(history, lookback=3, wr_threshold=20.0)
    assert any('WR' in f for f in flags)
    assert any('PnL' in f for f in flags)


def test_trend_flag_not_triggered_if_one_good_day_breaks_streak():
    history = [
        {'date': '2026-09-20', 'trades': 5, 'win_rate': 10.0, 'pnl_usd': -1.0},
        {'date': '2026-09-21', 'trades': 6, 'win_rate': 50.0, 'pnl_usd': 2.0},
        {'date': '2026-09-22', 'trades': 4, 'win_rate': 0.0, 'pnl_usd': -2.0},
    ]
    assert mod.detect_trend_flags(history, lookback=3, wr_threshold=20.0) == []


def test_trend_flag_not_triggered_with_insufficient_history():
    history = [{'date': '2026-09-22', 'trades': 4, 'win_rate': 0.0, 'pnl_usd': -2.0}]
    assert mod.detect_trend_flags(history, lookback=3) == []


# --- history persistence ---

def test_append_history_persists_and_trims(tmp_path, monkeypatch):
    history_file = tmp_path / 'history.json'
    monkeypatch.setattr(mod, 'HISTORY_FILE', str(history_file))
    monkeypatch.setattr(mod, 'CACHE_DIR', str(tmp_path))

    for i in range(35):
        mod._append_history(live_trades=i, live_wr=10.0, live_pnl=-1.0)

    history = mod._load_history()
    assert len(history) == 30  # auf die letzten 30 getrimmt
    assert history[-1]['trades'] == 34
