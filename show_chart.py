#!/usr/bin/env python3
"""
show_chart.py — Simuliert einen Envelope-Chart und sendet ihn per Telegram.

Laedt OHLCV-Daten, berechnet Envelope-Indikatoren und sendet einen PNG-Chart
mit MA, Envelope-Baendern und Entry/SL/TP-Levels.
Kein echter Trade wird platziert.

Aufruf:
    .venv/bin/python show_chart.py
    .venv/bin/python show_chart.py --symbol BTC/USDT:USDT --timeframe 4h
    .venv/bin/python show_chart.py --symbol BTC/USDT:USDT --timeframe 4h --side buy
"""
import argparse
import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

import pandas as pd

from ltbbot.utils.exchange import Exchange
from ltbbot.utils.trade_manager import _generate_ltbbot_chart_png
from ltbbot.utils.telegram import send_photo, send_message
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

logging.basicConfig(level=logging.WARNING, format='[%(levelname)s] %(message)s')

TMP_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')


def _load_secrets():
    with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
        return json.load(f)


def _load_settings():
    with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
        return json.load(f)


def _load_config(symbol: str, timeframe: str) -> dict:
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    for name in [f'config_{safe}_envelope.json', f'config_{safe}.json']:
        path = os.path.join(configs_dir, name)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return {}


def _make_dummy_signal(band_prices: dict, signal_side: str, stop_loss_pct: float) -> dict:
    """Baut ein Dummy-Signal aus Envelope-Bandpreisen."""
    if signal_side == 'buy':
        bands = band_prices.get('long', [])
        entry = float(bands[0]) if bands else float(band_prices.get('average', 0))
        sl    = entry * (1 - stop_loss_pct)
    else:
        bands = band_prices.get('short', [])
        entry = float(bands[0]) if bands else float(band_prices.get('average', 0))
        sl    = entry * (1 + stop_loss_pct)
    tp = float(band_prices.get('average', entry))
    rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 2.0
    return {'entry_price': entry, 'sl_price': sl, 'tp_price': tp, 'rr': rr}


def generate_and_send(exchange: Exchange, symbol: str, timeframe: str,
                      force_side: str, settings: dict, tg: dict) -> bool:
    config = _load_config(symbol, timeframe)

    # Build params — either from config or minimal fallback
    if config:
        params = config
        params.setdefault('market', {'symbol': symbol, 'timeframe': timeframe})
    else:
        params = {
            'market':   {'symbol': symbol, 'timeframe': timeframe},
            'strategy': {'average_type': 'EMA', 'average_period': 20,
                         'envelopes': [0.03, 0.06, 0.09]},
            'risk':     {'stop_loss_pct': 3.0, 'leverage': 10},
        }

    required = params.get('strategy', {}).get('average_period', 20) + 60
    print(f"  Lade OHLCV {symbol} ({timeframe})...")
    df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=required)
    if df is None or df.empty or len(df) < 30:
        print(f"  WARNUNG: Nicht genug Daten.")
        return False
    df = df.iloc[:-1]

    df_ind, band_prices = calculate_indicators_and_signals(df, params)

    regime = band_prices.get('regime', 'UNCERTAIN')
    trend  = band_prices.get('trend_direction', 'NEUTRAL')

    # Pick side based on trend (or force_side)
    if force_side:
        side = force_side
    elif trend == 'UPTREND':
        side = 'buy'
    elif trend == 'DOWNTREND':
        side = 'sell'
    else:
        side = 'buy'

    stop_loss_pct = params.get('risk', {}).get('stop_loss_pct', 3.0) / 100.0
    sig = _make_dummy_signal(band_prices, side, stop_loss_pct)
    entry, sl, tp, rr = sig['entry_price'], sig['sl_price'], sig['tp_price'], sig['rr']

    print(f"  Regime: {regime} | Trend: {trend} | Side: {side.upper()}")
    print(f"  Entry: {entry:.6g} | SL: {sl:.6g} | TP: {tp:.6g} | R:R 1:{rr:.1f}")

    os.makedirs(TMP_DIR, exist_ok=True)
    path = _generate_ltbbot_chart_png(df_ind, band_prices, side, entry, sl, tp, symbol, timeframe)

    if not path or not os.path.exists(path):
        print("  FEHLER: PNG konnte nicht erstellt werden.")
        return False

    side_label = 'LONG' if side == 'buy' else 'SHORT'
    caption = (
        f"[SIMULATION] LTBBOT | {symbol} ({timeframe})\n"
        f"{side_label} @ {entry:.6g}  |  SL: {sl:.6g}  |  TP: {tp:.6g}\n"
        f"R:R 1:{rr:.1f}  |  Regime: {regime} | Trend: {trend}"
    )
    send_photo(tg['bot_token'], tg['chat_id'], path, caption)
    os.remove(path)
    print("  Chart gesendet.")
    return True


def main():
    parser = argparse.ArgumentParser(description='ltbbot Envelope-Chart simulieren und per Telegram senden')
    parser.add_argument('--symbol',    type=str, help='Symbol (z.B. BTC/USDT:USDT)')
    parser.add_argument('--timeframe', type=str, help='Timeframe (z.B. 4h)')
    parser.add_argument('--side',      type=str, default='',
                        choices=['buy', 'sell', ''],
                        help='Richtung erzwingen (default: automatisch aus Trend)')
    args = parser.parse_args()

    secrets  = _load_secrets()
    settings = _load_settings()

    tg = secrets.get('telegram', {})
    if not tg.get('bot_token') or not tg.get('chat_id'):
        print("FEHLER: Kein Telegram-Token/Chat-ID in secret.json.")
        sys.exit(1)

    accounts = secrets.get('ltbbot', [])
    if not accounts:
        print("FEHLER: Kein 'ltbbot'-Account in secret.json.")
        sys.exit(1)

    print("Initialisiere Exchange...")
    exchange = Exchange(accounts[0])
    if not exchange.markets:
        print("FEHLER: Exchange konnte nicht initialisiert werden.")
        sys.exit(1)

    active = settings.get('live_trading_settings', {}).get('active_strategies', [])

    if args.symbol or args.timeframe:
        targets = [
            s for s in active
            if (not args.symbol    or s['symbol']    == args.symbol)
            and (not args.timeframe or s['timeframe'] == args.timeframe)
        ]
        if not targets:
            sym = args.symbol or (active[0]['symbol'] if active else 'BTC/USDT:USDT')
            tf  = args.timeframe or '4h'
            targets = [{'symbol': sym, 'timeframe': tf, 'active': True}]
    else:
        targets = [s for s in active if s.get('active', True)]

    if not targets:
        print("Keine passenden Strategien gefunden.")
        sys.exit(1)

    print(f"\n{len(targets)} Strategie(n) — generiere Charts...\n")
    send_message(tg['bot_token'], tg['chat_id'],
                 f"LTBBOT Chart-Simulation ({len(targets)} Strategie(n))")

    ok = 0
    for s in targets:
        symbol    = s.get('symbol', '')
        timeframe = s.get('timeframe', '')
        if not symbol or not timeframe:
            continue
        print(f"[{symbol} / {timeframe}]")
        try:
            if generate_and_send(exchange, symbol, timeframe, args.side, settings, tg):
                ok += 1
        except Exception as e:
            print(f"  FEHLER: {e}")

    print(f"\nFertig: {ok}/{len(targets)} Charts gesendet.")


if __name__ == '__main__':
    main()
