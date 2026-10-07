# src/ltbbot/utils/oos_window.py
"""Feste OOS-Woche + Training davor je Timeframe -- EINE Quelle fuer Pipeline (optimizer.py),
automatische Parameter-Suche und Portfolio-Optimizer (2026-10-06, User-Vorgabe):

    OOS      = die letzten `oos_weeks` Wochen bis heute (Standard 26) -- die aktuelle Phase
    Training = davor, Laenge je Timeframe aus `train_days_by_timeframe`

Aktiv, sobald settings.json::optimization_settings.oos_weeks gesetzt ist; sonst gilt die
bisherige Aufteilung (backtest_lookback_weeks + is_fraction)."""
from datetime import date, timedelta

# Tabelle der frueheren run_pipeline.sh (bis 2026-09-27), jetzt als Trainingslaenge VOR den OOS-Wochen
DEFAULT_TRAIN_DAYS = {'5m': 90, '15m': 90, '30m': 548, '1h': 548, '2h': 730,
                      '4h': 1095, '6h': 1095, '1d': 1825}


def oos_weeks(opt_settings):
    v = (opt_settings or {}).get('oos_weeks')
    return int(v) if v else None


def train_days(timeframe, opt_settings):
    table = (opt_settings or {}).get('train_days_by_timeframe') or {}
    return int(table.get(timeframe, DEFAULT_TRAIN_DAYS.get(timeframe, 384)))


def windows(timeframe, end_date, opt_settings):
    """end_date 'YYYY-MM-DD' (letzter Tag) -> (train_start, oos_start) als 'YYYY-MM-DD'.
    Training = [train_start, oos_start), OOS = [oos_start, end_date]."""
    end = date.fromisoformat(end_date)
    oos_start = end + timedelta(days=1) - timedelta(weeks=oos_weeks(opt_settings))
    train_start = oos_start - timedelta(days=train_days(timeframe, opt_settings))
    return train_start.isoformat(), oos_start.isoformat()
