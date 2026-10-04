# src/ltbbot/utils/breakout_trader.py
"""Live-Ausfuehrung des Band-Durchbruch-Modus (strategy.mode == 'breakout', 2026-10-04).

Spiegelt analysis/breakout_backtester.simulate_breakout_trades -- Signal, SL/TP, Groesse und
BTC-Filter kommen aus derselben Datei (strategy/breakout_logic.py):
  - Signal aus der letzten ABGESCHLOSSENEN Kerze; Einstieg nur im ersten 15-Min-Lauf nach
    Kerzenschluss (Backtest: Open der Folgekerze), sonst wird die Kerze ausgelassen.
  - Market-Einstieg mit an die Order gehaengtem SL/TP (keine Phase ohne Stop).
  - Zeitstopp: nach max_hold_candles Kerzen Market-Schliessung (Backtest: Open der Kerze).
  - Eine Position je Strategie/Symbol; kein Neueinstieg in der Kerze, in der geschlossen wurde.
  - Notfall: Position ohne ausreichende SL-Deckung -> Zwangsschliessung (check_naked_position).
"""
import logging
from datetime import datetime, timezone

import pandas as pd

from ltbbot.strategy.breakout_logic import (compute_breakout_indicators, breakout_signal, breakout_levels,
                                            position_size, regime_allows, btc_regime_from_daily)
from ltbbot.utils.exchange import drop_incomplete_last_candle
from ltbbot.utils.telegram import send_message

ENTRY_WINDOW_MIN = 20   # Einstieg nur bis 20 Min nach Kerzenschluss (erster Cron-Lauf, alle 15 Min)


def _tf_delta(timeframe):
    return pd.Timedelta(timeframe.replace('m', 'min')) if timeframe.endswith('m') else pd.Timedelta(timeframe)


def _btc_regime(exchange, sma_days):
    raw = exchange.fetch_recent_ohlcv('BTC/USDT:USDT', '1d', limit=sma_days + 40)
    return btc_regime_from_daily(drop_incomplete_last_candle(raw), sma_days)


def full_breakout_cycle(exchange, params, telegram_config, logger, tracker_file_path, read_tracker, write_tracker,
                        check_naked_position, now=None):
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    sp = params['strategy']
    tg = (telegram_config.get('bot_token'), telegram_config.get('chat_id'))
    now = now or datetime.now(timezone.utc)
    logger.info(f"===== Breakout-Zyklus {symbol} ({timeframe}) =====")

    raw = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=int(sp.get('average_period', 20)) + 80)
    df = drop_incomplete_last_candle(raw)
    if df is None or len(df) < int(sp.get('average_period', 20)) + 20:
        logger.warning(f"Zu wenige Kerzen fuer {symbol} ({timeframe}) -- Zyklus uebersprungen.")
        return
    ind = compute_breakout_indicators(df, params)
    sig = ind.iloc[-1]                         # letzte ABGESCHLOSSENE Kerze
    cur_open = ind.index[-1] + _tf_delta(timeframe)   # Beginn der laufenden Kerze
    tracker = read_tracker(tracker_file_path) or {}

    # --- Notfall-Check: offene Position ohne SL-Deckung -> zwangsschliessen ---
    if check_naked_position(exchange, symbol, tracker_file_path, telegram_config, logger):
        logger.critical(f"Notfall-Schliessung {symbol} -- Zyklus beendet.")
        return

    positions = [p for p in exchange.fetch_open_positions(symbol) if float(p.get('contracts') or 0) > 0]
    pos = positions[0] if positions else None

    if pos is not None:
        entry_candle = tracker.get('bo_entry_candle')
        if entry_candle:
            held = (cur_open - pd.Timestamp(entry_candle)) / _tf_delta(timeframe)
            logger.info(f"Position {pos.get('side')} {pos.get('contracts')} offen seit {held:.0f} Kerzen "
                        f"(Zeitstopp bei {int(sp.get('max_hold_candles', 60))}).")
            if held >= int(sp.get('max_hold_candles', 60)):
                exchange.cancel_position_tpsl_orders(symbol)
                close_side = 'sell' if pos.get('side') == 'long' else 'buy'
                exchange.place_market_order(symbol, close_side, float(pos['contracts']), reduce=True)
                tracker.update({'bo_entry_candle': None, 'bo_closed_candle': str(cur_open)})
                write_tracker(tracker_file_path, tracker)
                send_message(*tg, f"⏱ ZEITSTOPP {symbol} ({timeframe})\nPosition nach {held:.0f} Kerzen geschlossen.")
        else:
            logger.warning(f"Offene Position {symbol} ohne Tracker-Eintrag -- nur Notfall-Schutz aktiv.")
        return

    # --- flach ---
    if tracker.get('bo_entry_candle'):
        logger.info(f"Position {symbol} wurde geschlossen (SL/TP an der Boerse).")
        tracker.update({'bo_entry_candle': None, 'bo_closed_candle': str(cur_open)})
        write_tracker(tracker_file_path, tracker)
    if tracker.get('bo_closed_candle') == str(cur_open):
        logger.info("In dieser Kerze bereits geschlossen -- kein Neueinstieg (wie Backtest).")
        return
    if tracker.get('bo_last_signal_candle') == str(sig.name):
        logger.debug("Signalkerze bereits ausgewertet.")
        return
    tracker['bo_last_signal_candle'] = str(sig.name)
    write_tracker(tracker_file_path, tracker)

    late = (pd.Timestamp(now) - cur_open).total_seconds() / 60.0
    side = breakout_signal(sig['close'], sig['average'], sig['atr'], params)
    if side is None:
        logger.info(f"Kein Ausbruch: Close {sig['close']:.6g}, Band {sig['average'] + float(sp.get('band_atr', 3)) * sig['atr']:.6g}.")
        return
    if late > ENTRY_WINDOW_MIN:
        logger.warning(f"Ausbruch {side} erkannt, aber {late:.0f} Min nach Kerzenbeginn -- ausgelassen (Backtest: Einstieg zum Open).")
        return
    if sp.get('use_btc_filter', True):
        regime = _btc_regime(exchange, int(sp.get('btc_sma_days', 200)))
        if not regime_allows(side, sig.name, regime, params):
            logger.info(f"Ausbruch {side}, aber BTC-Filter verbietet den Einstieg (BTC nicht ueber SMA{sp.get('btc_sma_days', 200)}).")
            return

    price = float(exchange.fetch_ticker(symbol)['last'])
    sl, tp = breakout_levels(side, price, float(sig['atr']), params)
    balance = exchange.fetch_balance_usdt()
    sz = position_size(balance, price, sl, params)
    if not sz:
        logger.warning(f"Position zu klein (<5 USDT) oder keine freie Margin (Guthaben {balance:.2f}) -- ausgelassen.")
        return
    amount, margin = sz
    rp = params['risk']
    try:
        exchange.set_margin_mode(symbol, margin_mode=rp.get('margin_mode', 'isolated'))
        exchange.set_leverage(symbol, margin_mode=rp.get('margin_mode', 'isolated'), leverage=int(rp.get('leverage', 5)))
    except Exception as e:
        logger.warning(f"Margin/Hebel nicht gesetzt (evtl. schon korrekt): {e}")
    order_side = 'buy' if side == 'long' else 'sell'
    exchange.place_market_entry_with_sltp(symbol, order_side, amount, sl, tp)
    tracker.update({'bo_entry_candle': str(cur_open), 'bo_side': side, 'bo_sl': sl, 'bo_tp': tp})
    write_tracker(tracker_file_path, tracker)
    send_message(*tg, f"🚀 BREAKOUT {side.upper()} {symbol} ({timeframe})\n"
                      f"Einstieg ~{price:.6g} | SL {sl:.6g} | TP {tp:.6g}\n"
                      f"Menge {amount:.6g} | Margin {margin:.2f} USDT | Hebel {rp.get('leverage')}x\n"
                      f"Zeitstopp nach {sp.get('max_hold_candles')} Kerzen")
    logger.info(f"✅ Breakout-Einstieg {side} {symbol}: {amount:.6g} @ ~{price:.6g}, SL {sl:.6g}, TP {tp:.6g}")
