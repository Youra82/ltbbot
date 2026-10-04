# src/ltbbot/utils/lookback.py
"""Rueckblick-Zeitraum je Timeframe -- EINE Quelle fuer Pipeline, automatische
Parameter-Suche und sync_confirmed_flags.py (2026-10-04). Vorher galten pauschal
backtest_lookback_weeks (26) fuer alle Timeframes: auf 4h/6h ergab das nur ~10 OOS-Trades,
und auf 3 Jahren gefundene Configs waeren beim Nachpruefen auf 26 Wochen wieder
herausgefallen."""
from datetime import date, timedelta

# Richtwerte der frueheren run_pipeline.sh-Tabelle
DEFAULT_LOOKBACK_DAYS = {'5m': 90, '15m': 90, '30m': 548, '1h': 548, '2h': 730,
                         '4h': 1095, '6h': 1095, '1d': 1825}


def lookback_days(timeframe, opt_settings):
    table = opt_settings.get('lookback_days_by_timeframe') or {}
    if timeframe in table:
        return int(table[timeframe])
    if table or 'backtest_lookback_weeks' not in opt_settings:
        return DEFAULT_LOOKBACK_DAYS.get(timeframe, 548)
    return int(opt_settings['backtest_lookback_weeks']) * 7


def lookback_start_date(timeframe, end_date, opt_settings):
    """end_date: 'YYYY-MM-DD' -> Startdatum als 'YYYY-MM-DD'."""
    return (date.fromisoformat(end_date) - timedelta(days=lookback_days(timeframe, opt_settings))).isoformat()
