# tests/test_daily_live_vs_backtest_check.py
"""Lokale Tests fuer den Tagescheck (keine Bitget-Aufrufe). Ersetzt die Tests des alten
Summen-je-Coin-Checks (2026-10-23)."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import pandas as pd

import daily_live_vs_backtest_check as mod

LOCAL_TZ = datetime.now().astimezone().tzinfo
CANDLE = pd.Timestamp('2026-10-22 08:00', tz='UTC')  # Beginn der Fuellkerze (4h)
T = lambda s: pd.Timestamp(s, tz='UTC')


def _write_log(tmp_path, key, entries):
    """entries: (Minuten nach Kerzenbeginn, Level, Meldung) -- Zeitstempel in Ortszeit wie RotatingFileHandler."""
    lines = []
    for minutes, level, msg in entries:
        ts = (CANDLE + pd.Timedelta(minutes=minutes)).tz_convert(LOCAL_TZ)
        lines.append(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},123 - {level} - {msg}\n")
    (tmp_path / f'ltbbot_{key}.log').write_text(''.join(lines), encoding='utf-8')


def test_position_limit_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    _write_log(tmp_path, 'HBARUSDTUSDT_4h', [
        (0.5, 'INFO', 'Keine offene Position für HBAR/USDT:USDT.'),
        (0.6, 'INFO', '10 Positionen offen (Grenze 10) -- keine neuen Einstiege fuer HBAR/USDT:USDT.'),
    ])
    assert mod.explain_from_log('HBARUSDTUSDT_4h', CANDLE).startswith('Positionsgrenze erreicht')


def test_orders_placed_but_not_filled(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    _write_log(tmp_path, 'XVGUSDTUSDT_4h', [
        (0.5, 'INFO', '✅ Long Entry 1/3 platziert: Amount=1000.0000, Trigger@0.0050, Limit@0.0050'),
        (15.5, 'INFO', 'Keine offene Position für XVG/USDT:USDT.'),
    ])
    assert mod.explain_from_log('XVGUSDTUSDT_4h', CANDLE).startswith('Einstiegs-Orders lagen, live nicht gefüllt')


def test_margin_and_btc_filter_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    _write_log(tmp_path, 'ADAUSDTUSDT_4h', [
        (0.5, 'WARNING', '⚠️ Long Layer 1: Margin 3.10 USDT wuerde freies Guthaben (1.20 USDT) uebersteigen. Überspringe.')])
    assert mod.explain_from_log('ADAUSDTUSDT_4h', CANDLE).startswith('zu wenig freie Margin')
    _write_log(tmp_path, 'SOLUSDTUSDT_4h', [
        (0.5, 'INFO', 'short-Einstiege fuer SOL/USDT:USDT gesperrt (BTC-Trend oder Sperre nach SL).')])
    assert mod.explain_from_log('SOLUSDTUSDT_4h', CANDLE).startswith('Einstiege gesperrt (BTC-Trend')


def test_bot_not_running_and_missing_log(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    _write_log(tmp_path, 'DOGEUSDTUSDT_4h', [(-600, 'INFO', 'Keine offene Position für DOGE/USDT:USDT.')])
    assert mod.explain_from_log('DOGEUSDTUSDT_4h', CANDLE).startswith('Bot lief in dem Zeitraum nicht')
    assert mod.explain_from_log('ETHUSDTUSDT_4h', CANDLE) == 'kein Bot-Log vorhanden'


def test_bands_grouped_into_positions():
    layers = [  # zwei Baender einer Long-Position, dann eine neue Short-Position
        dict(side='long', entry_time=T('2026-10-20 08:00'), exit_time=T('2026-10-20 16:00'),
             entry=10.0, exit=10.5, amount=1.0, pnl=0.5),
        dict(side='long', entry_time=T('2026-10-20 12:00'), exit_time=T('2026-10-20 16:00'),
             entry=9.0, exit=10.5, amount=3.0, pnl=4.5),
        dict(side='short', entry_time=T('2026-10-21 08:00'), exit_time=None,
             entry=11.0, exit=None, amount=1.0, pnl=None),
    ]
    pos = mod.group_positions(layers, 'HBARUSDTUSDT_4h', T('2026-10-23 00:00'))
    assert len(pos) == 2
    p = pos[0]
    assert (p['side'], p['bands'], p['signal_time'], p['exit_time']) == ('long', 2, T('2026-10-20 08:00'),
                                                                          T('2026-10-20 16:00'))
    assert abs(p['entry'] - 9.25) < 1e-9 and abs(p['exit'] - 10.5) < 1e-9 and abs(p['pnl'] - 5.0) < 1e-9
    assert pos[1]['side'] == 'short' and pos[1]['exit_time'] is None and pos[1]['pnl'] is None


def test_new_position_after_full_close():
    layers = [dict(side='long', entry_time=T('2026-10-20 08:00'), exit_time=T('2026-10-20 12:00'),
                   entry=10.0, exit=10.2, amount=1.0, pnl=0.2),
              dict(side='long', entry_time=T('2026-10-20 16:00'), exit_time=T('2026-10-20 20:00'),
                   entry=10.1, exit=10.3, amount=1.0, pnl=0.2)]
    assert len(mod.group_positions(layers, 'HBARUSDTUSDT_4h', T('2026-10-23 00:00'))) == 2


def test_entry_triggers_are_not_orphans():
    plans = [  # Einstiegs-Trigger werden schon in fetch_live verworfen; hier nur Schutz-Orders
        dict(symbol='ADAUSDT', planType='normal_plan', triggerPrice='0.30', cTime='1760000000000'),
        dict(symbol='ADAUSDT', planType='normal_plan', triggerPrice='0.30', cTime='1760000000000'),
    ]
    flags = mod.check_orders([], plans)
    assert any('Verwaiste' in f for f in flags) and any('2x gleiche' in f for f in flags)
    assert mod.check_orders([{'symbol': 'ADAUSDT'}], plans[:1]) == []


def test_reason_respects_side(tmp_path, monkeypatch):
    """VPS 23.10.: fehlender BAT-Long wurde mit 'short-Einstiege gesperrt' erklaert."""
    monkeypatch.setattr(mod, 'LOG_DIR', str(tmp_path))
    _write_log(tmp_path, 'BATUSDTUSDT_2h', [
        (0.5, 'INFO', 'short-Einstiege fuer BAT/USDT:USDT gesperrt (BTC-Trend oder Sperre nach SL).'),
        (0.6, 'INFO', 'Prüfe Long Entry Bands: [0.1, 0.09]'),
        (0.7, 'INFO', 'Long Layer 1: Kein Close-Confirmation (Close 0.12 > Band 0.1). Überspringe.'),
    ])
    assert mod.explain_from_log('BATUSDTUSDT_2h', CANDLE, 'long').startswith('keine Close-Bestätigung')
    assert mod.explain_from_log('BATUSDTUSDT_2h', CANDLE, 'short').startswith('Einstiege gesperrt (BTC-Trend')


def test_reentry_while_sim_position_still_open():
    """VPS 23.10.: ARB live geschlossen und neu eroeffnet, Simulator-Position noch offen."""
    lt_first = dict(symbol='ARBUSDT', side='long', entry_time=T('2026-10-22 05:31'), exit_time=T('2026-10-23 10:00'),
                    entry=0.30, exit=0.31, pnl=0.2)
    st = dict(key='ARBUSDTUSDT_6h', symbol='ARBUSDT', side='long', signal_time=T('2026-10-22 00:00'),
              exit_time=None, entry=0.30, exit=None, pnl=None, bands=2)
    lt_new = dict(symbol='ARBUSDT', side='long', entry_time=T('2026-10-23 14:17'), exit_time=None,
                  entry=0.31, exit=None, pnl=0.0)
    items = mod.build_items([(lt_first, st)], [(lt_new, ['ARBUSDTUSDT_6h'])], [], [lt_first, lt_new],
                            T('2026-10-23 20:00'))
    extra = [d for d in items if d['status'] == 'nur_live'][0]
    assert extra['reason'].startswith('Wiedereinstieg') and 'dort noch offen' in extra['reason']
