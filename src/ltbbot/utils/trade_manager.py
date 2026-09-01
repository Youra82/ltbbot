# src/ltbbot/utils/trade_manager.py
import logging
import time
import ccxt
import os
import json
from datetime import datetime
import sys
import pandas as pd # Hinzugefügt für pd.isna Check
import ta  # Für ATR-Berechnung

# Pfade für die Tracker-Datei definieren
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TRACKER_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker')

# Sicherstellen, dass das src-Verzeichnis im PYTHONPATH ist (kann in manchen Setups helfen)
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.telegram import send_message, send_photo
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals
from ltbbot.utils.exchange import Exchange, drop_incomplete_last_candle # Import hinzugefügt, falls Type Hinting verwendet wird (optional)


# --- Chart-Generierung ---

def _generate_ltbbot_chart_png(df: pd.DataFrame, band_prices: dict, signal_side: str,
                                entry_price: float, sl_price: float, tp_price: float,
                                symbol: str, timeframe: str, n_candles: int = 60) -> str:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import uuid

    df_plot = df.tail(n_candles).reset_index(drop=True)
    n = len(df_plot)

    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor('#0d1117')
    ax.set_facecolor('#0d1117')

    # Candlesticks
    for i in range(n):
        row = df_plot.iloc[i]
        o, h, l, c = float(row['open']), float(row['high']), float(row['low']), float(row['close'])
        bull = c >= o
        body_color = '#26a69a' if bull else '#ef5350'
        wick_color = '#4cceac' if bull else '#ff6b6b'
        ax.plot([i, i], [l, h], color=wick_color, linewidth=0.8)
        ax.add_patch(plt.Rectangle((i - 0.35, min(o, c)), 0.7, abs(c - o),
                                    color=body_color, zorder=2))

    # Moving Average (envelope center = TP target)
    if 'average' in df_plot.columns:
        avg_vals = pd.to_numeric(df_plot['average'], errors='coerce')
        ax.plot(range(n), avg_vals, color='#00bcd4', linewidth=1.3, label='MA', zorder=3)

    # Envelope bands as dashed lines
    band_colors = ['#7b68ee', '#ba55d3', '#c71585', '#ff69b4']
    band_idx = 0
    while True:
        b = band_idx + 1
        lo_col = f'band_low_{b}'
        hi_col = f'band_high_{b}'
        if lo_col not in df_plot.columns:
            break
        color = band_colors[band_idx % len(band_colors)]
        lo_vals = pd.to_numeric(df_plot[lo_col], errors='coerce')
        hi_vals = pd.to_numeric(df_plot[hi_col], errors='coerce')
        ax.plot(range(n), lo_vals, color=color, linewidth=0.8, linestyle='--', alpha=0.65)
        ax.plot(range(n), hi_vals, color=color, linewidth=0.8, linestyle='--', alpha=0.65)
        band_idx += 1

    # Entry / SL / TP horizontal lines
    ax.axhline(entry_price, color='#ffd700', linewidth=1.0, linestyle='--', zorder=4)
    ax.axhline(sl_price,    color='#ef5350', linewidth=1.2, linestyle='-',  zorder=4)
    ax.axhline(tp_price,    color='#26a69a', linewidth=1.2, linestyle='-',  zorder=4)

    # Price tags (right side)
    x_tag = n + 0.3
    fmt = '{:.6g}'
    for price, label, color in [
        (tp_price,    f'TP {fmt.format(tp_price)}',    '#26a69a'),
        (entry_price, f'Entry {fmt.format(entry_price)}', '#ffd700'),
        (sl_price,    f'SL {fmt.format(sl_price)}',    '#ef5350'),
    ]:
        ax.text(x_tag, price, label,
                color=color, fontsize=7.5, va='center', ha='left',
                bbox=dict(facecolor='#0d1117', edgecolor=color, boxstyle='round,pad=0.2', alpha=0.85),
                zorder=6)

    # Infobox
    regime = band_prices.get('regime', '')
    trend  = band_prices.get('trend_direction', '')
    adx    = band_prices.get('adx')
    sl_pct = abs(entry_price - sl_price) / entry_price * 100 if entry_price > 0 else 0
    tp_pct = abs(tp_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
    rr     = tp_pct / sl_pct if sl_pct > 0 else 0.0
    adx_str = f'  ADX: {adx:.1f}' if adx is not None else ''
    side_label = 'LONG' if signal_side == 'buy' else 'SHORT'
    side_color = '#26a69a' if signal_side == 'buy' else '#ef5350'

    info_text = '\n'.join([
        f'{side_label}   R:R 1:{rr:.1f}',
        f'Regime: {regime} | Trend: {trend}{adx_str}',
        f'Signal: Envelope Mean-Reversion',
        f'SL: {sl_pct:.2f}%  TP: {tp_pct:.2f}% (MA)',
    ])
    ax.text(0.01, 0.98, info_text, transform=ax.transAxes,
            fontsize=7.5, verticalalignment='top', color='white',
            bbox=dict(facecolor='#1e2530', edgecolor=side_color, boxstyle='round,pad=0.5', alpha=0.9),
            zorder=7)

    sym_clean = symbol.split('/')[0] if '/' in symbol else symbol
    ax.set_title(f'{sym_clean} / {timeframe} — {side_label} Setup (Envelope)',
                 color='white', fontsize=11, pad=8)
    ax.tick_params(colors='#aaaaaa')
    ax.set_xlim(-1, n + 9)
    for spine in ax.spines.values():
        spine.set_edgecolor('#333333')
    ax.grid(axis='y', color='#2a2a2a', linewidth=0.5)
    ax.set_xticks([])
    plt.tight_layout()

    tmp_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, f'ltbbot_chart_{uuid.uuid4().hex[:8]}.png')
    plt.savefig(path, dpi=130, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def _send_ltbbot_chart(df: pd.DataFrame, band_prices: dict, signal_side: str,
                        entry_price: float, sl_price: float, tp_price: float,
                        symbol: str, timeframe: str, telegram_config: dict,
                        logger: logging.Logger):
    try:
        path = _generate_ltbbot_chart_png(df, band_prices, signal_side, entry_price,
                                           sl_price, tp_price, symbol, timeframe)
        if not path or not os.path.exists(path):
            return
        sl_pct = abs(entry_price - sl_price) / entry_price * 100 if entry_price > 0 else 0
        tp_pct = abs(tp_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        rr = tp_pct / sl_pct if sl_pct > 0 else 0.0
        side_label = 'LONG' if signal_side == 'buy' else 'SHORT'
        caption = (
            f"LTBBOT | {symbol} ({timeframe})\n"
            f"{side_label} @ {entry_price:.6g}  |  SL: {sl_price:.6g}  |  TP: {tp_price:.6g}\n"
            f"R:R 1:{rr:.1f}  |  Envelope Mean-Reversion"
        )
        send_photo(telegram_config.get('bot_token'), telegram_config.get('chat_id'), path, caption)
        os.remove(path)
    except Exception as e:
        logger.warning(f"Fehler beim Senden des Envelope-Charts: {e}")


# --- Performance Tracking ---

def update_performance_stats(tracker_file_path, trade_result, logger):
    """
    Aktualisiert Performance-Statistiken nach jedem Trade.
    
    Args:
        tracker_file_path: Pfad zur Tracker-Datei
        trade_result: 'win' oder 'loss'
        logger: Logger-Instanz
    """
    tracker_info = read_tracker_file(tracker_file_path)
    
    # Initialisiere Performance-Stats falls nicht vorhanden
    if 'performance' not in tracker_info:
        tracker_info['performance'] = {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'consecutive_losses': 0,
            'consecutive_wins': 0,
            'max_consecutive_losses': 0
        }
    
    perf = tracker_info['performance']
    perf['total_trades'] += 1
    
    if trade_result == 'win':
        perf['winning_trades'] += 1
        perf['consecutive_wins'] += 1
        perf['consecutive_losses'] = 0
    else:
        perf['losing_trades'] += 1
        perf['consecutive_losses'] += 1
        perf['consecutive_wins'] = 0
        perf['max_consecutive_losses'] = max(perf['max_consecutive_losses'], perf['consecutive_losses'])
    
    # Berechne Win-Rate
    if perf['total_trades'] > 0:
        win_rate = (perf['winning_trades'] / perf['total_trades']) * 100
        perf['win_rate'] = win_rate
        
        # Warnung bei schlechter Performance
        if perf['total_trades'] >= 30 and win_rate < 30:
            logger.warning(f"⚠️ SCHLECHTE PERFORMANCE: Win-Rate {win_rate:.1f}% nach {perf['total_trades']} Trades")
    
    update_tracker_file(tracker_file_path, tracker_info)

def should_reduce_risk(tracker_file_path):
    """
    Prüft ob Risiko reduziert werden sollte basierend auf Performance.
    
    Returns:
        tuple: (reduce_risk: bool, reason: str)
    """
    tracker_info = read_tracker_file(tracker_file_path)
    
    if 'performance' not in tracker_info:
        return False, "Keine Performance-Daten"
    
    perf = tracker_info['performance']
    
    # Risiko-Reduktion bei:
    # 1. 5+ aufeinanderfolgende Verluste
    if perf.get('consecutive_losses', 0) >= 5:
        return True, f"5+ aufeinanderfolgende Verluste ({perf['consecutive_losses']})"
    
    # 2. Win-Rate < 25% nach mindestens 30 Trades
    if perf.get('total_trades', 0) >= 30:
        win_rate = perf.get('win_rate', 50)
        if win_rate < 25:
            return True, f"Win-Rate zu niedrig: {win_rate:.1f}%"
    
    return False, "Performance OK"

# --- ATR-basierte Stop-Loss Anpassung ---

def calculate_atr_adjusted_stop_loss(exchange: Exchange, symbol: str, base_sl_pct: float, logger: logging.Logger):
    """
    Berechnet einen ATR-basierten Stop-Loss, der sich an die aktuelle Marktvolatilität anpasst.
    
    Args:
        exchange: Exchange-Instanz
        symbol: Trading-Symbol (z.B. 'BTC/USDT:USDT')
        base_sl_pct: Basis Stop-Loss in Prozent (aus Config)
        logger: Logger-Instanz
    
    Returns:
        float: Angepasster Stop-Loss in Prozent (z.B. 0.015 für 1.5%)
    """
    try:
        # Timeframe dynamisch aus Symbol ableiten (z.B. "SOL/USDT:USDT (30m)" → "30m")
        # Annahme: Symbol ist im Format "COIN/USDT:USDT" und Timeframe wird separat übergeben
        # Hole Timeframe aus Symbol falls vorhanden, sonst Default "30m"
        timeframe = "30m"
        if ":" in symbol and "_" in symbol:
            # z.B. "SOLUSDTUSDT_30m" → "30m"
            parts = symbol.split("_")
            if len(parts) > 1:
                timeframe = parts[-1]
        
        # Hole aktuelle Kerzen für ATR-Berechnung (14 Perioden + etwas Buffer)
        ohlcv_df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=50)
        ohlcv_df = drop_incomplete_last_candle(ohlcv_df)
        if ohlcv_df is None or len(ohlcv_df) < 14:
            logger.warning(f"Nicht genug Daten für ATR-Berechnung. Verwende Basis-SL: {base_sl_pct*100:.2f}%")
            return base_sl_pct
        
        # Berechne ATR (14 Perioden Standard)
        atr_value = ta.volatility.average_true_range(
            high=ohlcv_df['high'],
            low=ohlcv_df['low'],
            close=ohlcv_df['close'],
            window=14
        ).iloc[-1]
        current_price = ohlcv_df['close'].iloc[-1]
        atr_pct = (atr_value / current_price)
        atr_multiplier = 2.0  # Kann in Config konfigurierbar gemacht werden
        atr_based_sl = atr_pct * atr_multiplier
        min_sl = base_sl_pct * 0.8  # Mindestens 80% des Basis-SL
        max_sl = base_sl_pct * 3.0  # Maximal 3x Basis-SL
        adjusted_sl = max(min_sl, min(atr_based_sl, max_sl))
        logger.info(f"📊 ATR Stop-Loss Anpassung:")
        logger.info(f"   ATR: {atr_value:.4f} ({atr_pct*100:.2f}% vom Preis)")
        logger.info(f"   Basis-SL: {base_sl_pct*100:.2f}% → ATR-basiert: {atr_based_sl*100:.2f}%")
        logger.info(f"   Finaler SL: {adjusted_sl*100:.2f}% (Min: {min_sl*100:.2f}%, Max: {max_sl*100:.2f}%)")
        return adjusted_sl
    except Exception as e:
        logger.error(f"Fehler bei ATR-Berechnung: {e}. Verwende Basis-SL.")
        return base_sl_pct

# --- Tracker File Handling ---

def get_tracker_file_path(symbol, timeframe):
    """Generiert den Pfad zur Tracker-Datei für eine Strategie."""
    os.makedirs(TRACKER_DIR, exist_ok=True) # Stelle sicher, dass das Verzeichnis existiert
    safe_filename = f"{symbol.replace('/', '-').replace(':', '-')}_{timeframe}.json"
    return os.path.join(TRACKER_DIR, safe_filename)

def read_tracker_file(file_path):
    """Liest den Status aus der Tracker-Datei."""
    default_data = {"status": "ok_to_trade", "last_side": None, "stop_loss_ids": []}
    if not os.path.exists(file_path):
        try: # Versuch, Standard zu schreiben
            with open(file_path, 'w') as f:
                json.dump(default_data, f, indent=4)
            logging.info(f"Initiale Tracker-Datei erstellt: {file_path}")
        except Exception as write_err:
            logging.error(f"Konnte initiale Tracker-Datei nicht schreiben {file_path}: {write_err}")
        return default_data
    try:
        with open(file_path, 'r') as f:
            # Füge zusätzliche Prüfung hinzu, ob die Datei leer ist
            content = f.read()
            if not content:
                logging.warning(f"Tracker-Datei {file_path} ist leer. Setze auf Standard zurück.")
                # Versuche, die leere Datei mit Standardwerten zu überschreiben
                try:
                    with open(file_path, 'w') as fw:
                        json.dump(default_data, fw, indent=4)
                except Exception as write_err:
                     logging.error(f"Konnte leere Tracker-Datei nicht überschreiben {file_path}: {write_err}")
                return default_data
            return json.loads(content)
    except (json.JSONDecodeError, FileNotFoundError):
        logging.error(f"Fehler beim Lesen oder Parsen der Tracker-Datei {file_path}. Setze auf Standard zurück.")
        try: # Versuch, korrupte Datei zu überschreiben
            with open(file_path, 'w') as f:
                json.dump(default_data, f, indent=4)
        except Exception as write_err:
            logging.error(f"Konnte korrupte Tracker-Datei nicht überschreiben {file_path}: {write_err}")
        return default_data
    except Exception as e:
         logging.error(f"Unerwarteter Fehler beim Lesen von {file_path}: {e}")
         return default_data


def update_tracker_file(file_path, data):
    """Schreibt den Status in die Tracker-Datei."""
    try:
        # Stelle sicher, dass das Verzeichnis existiert
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.debug(f"Tracker-Datei aktualisiert: {file_path} mit Daten: {data}")
    except Exception as e:
        logging.error(f"Fehler beim Schreiben der Tracker-Datei {file_path}: {e}")

# --- Order Management ---

def check_and_notify_new_position(exchange: Exchange, position: dict, params: dict, tracker_file_path: str, telegram_config: dict, logger: logging.Logger):
    """
    Prüft, ob eine Position NEU eröffnet wurde und sendet eine detaillierte Telegram-Benachrichtigung.
    Diese Funktion wird jedes Mal aufgerufen, wenn eine offene Position gefunden wird.
    Sie prüft den Tracker-Status, um festzustellen, ob es sich um eine NEUE Position handelt.
    
    Args:
        exchange: Exchange-Instanz
        position: Die aktuelle offene Position (dict)
        params: Trading-Parameter
        tracker_file_path: Pfad zur Tracker-Datei
        telegram_config: Telegram-Konfiguration
        logger: Logger-Instanz
    """
    try:
        tracker_info = read_tracker_file(tracker_file_path)
        symbol = params['market']['symbol']
        timeframe = params['market']['timeframe']
        account_name = exchange.account.get('name', 'Standard-Account')
        
        # Prüfe ob diese Position bereits gemeldet wurde
        # Verwende Entry-Preis und Seite als Identifikator
        current_entry_price = float(position.get('entryPrice', 0))
        current_side = position.get('side', '')
        current_contracts = float(position.get('contracts', 0))
        
        # Hole die zuletzt gemeldete Position aus dem Tracker
        last_notified_entry = tracker_info.get('last_notified_entry_price')
        last_notified_side = tracker_info.get('last_notified_side')
        
        # Wenn die Position neu ist (anderer Entry-Preis oder andere Seite)
        is_new_position = (
            last_notified_entry is None or 
            last_notified_side is None or
            abs(current_entry_price - last_notified_entry) > (current_entry_price * 0.001) or  # 0.1% Toleranz
            current_side != last_notified_side
        )
        
        if is_new_position:
            # Hole zusätzliche Position-Informationen
            unrealized_pnl = position.get('unrealizedPnl', 0)
            liquidation_price = position.get('liquidationPrice', 0)
            leverage = position.get('leverage', params['risk'].get('leverage', 1))
            margin_used = position.get('initialMargin', 0)
            
            # Hole TP und SL Preise aus offenen Orders
            tp_price = None
            sl_price = None
            try:
                open_triggers = exchange.fetch_open_trigger_orders(symbol)
                for order in open_triggers:
                    if order.get('reduceOnly'):
                        trigger_price = order.get('triggerPrice') or order.get('stopPrice')
                        order_side = order.get('side', '')
                        # Für Long-Position: TP=sell (über Entry), SL=sell (unter Entry)
                        # Für Short-Position: TP=buy (unter Entry), SL=buy (über Entry)
                        if trigger_price:
                            trigger_price = float(trigger_price)
                            if current_side == 'long' and order_side == 'sell':
                                if trigger_price > current_entry_price:
                                    tp_price = trigger_price
                                elif trigger_price < current_entry_price:
                                    sl_price = trigger_price
                            elif current_side == 'short' and order_side == 'buy':
                                if trigger_price < current_entry_price:
                                    tp_price = trigger_price
                                elif trigger_price > current_entry_price:
                                    sl_price = trigger_price
            except Exception as e:
                logger.warning(f"Konnte TP/SL-Preise nicht abrufen: {e}")
            
            # Erstelle detaillierte Nachricht
            side_emoji = "🟢" if current_side == 'long' else "🔴"
            message = f"{side_emoji} *NEUE POSITION ERÖFFNET*\n\n"
            message += f"💼 Account: {account_name}\n"
            message += f"📊 Symbol: {symbol}\n"
            message += f"⏱ Timeframe: {timeframe}\n"
            message += f"📈 Richtung: {current_side.upper()}\n"
            message += f"📦 Menge: {current_contracts:.4f} Kontrakte\n"
            message += f"💵 Entry-Preis: {current_entry_price:.6f} USDT\n"
            message += f"⚡️ Hebel: {leverage}x\n"
            message += f"💰 Margin verwendet: {margin_used:.2f} USDT\n"
            
            if tp_price:
                tp_distance_pct = abs((tp_price - current_entry_price) / current_entry_price * 100)
                message += f"🎯 Take-Profit: {tp_price:.6f} USDT (+{tp_distance_pct:.2f}%)\n"
            else:
                message += f"🎯 Take-Profit: Nicht gefunden\n"
            
            if sl_price:
                sl_distance_pct = abs((sl_price - current_entry_price) / current_entry_price * 100)
                message += f"🛑 Stop-Loss: {sl_price:.6f} USDT (-{sl_distance_pct:.2f}%)\n"
            else:
                message += f"🛑 Stop-Loss: Nicht gefunden\n"
            
            if tp_price and sl_price:
                risk_reward = abs(tp_price - current_entry_price) / abs(current_entry_price - sl_price)
                message += f"⚖️ Risk/Reward: 1:{risk_reward:.2f}\n"
            
            message += f"\n📉 Unreal. P&L: {unrealized_pnl:.2f} USDT\n"
            
            if liquidation_price and liquidation_price > 0:
                message += f"⚠️ Liquidation: {liquidation_price:.6f} USDT\n"
            
            message += f"\n🕐 Zeit: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            
            # Sende Telegram-Nachricht
            send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
            logger.info(f"✅ Telegram-Benachrichtigung für NEUE Position gesendet: {current_side} {current_contracts:.4f} @ {current_entry_price:.6f}")
            
            # Aktualisiere Tracker mit der gemeldeten Position
            tracker_info['last_notified_entry_price'] = current_entry_price
            tracker_info['last_notified_side'] = current_side
            tracker_info['last_notified_timestamp'] = datetime.now().isoformat()
            update_tracker_file(tracker_file_path, tracker_info)
        else:
            logger.debug(f"Position bereits gemeldet. Keine neue Benachrichtigung erforderlich.")
            
    except Exception as e:
        logger.error(f"Fehler beim Prüfen/Benachrichtigen neuer Position: {e}", exc_info=True)


def cancel_strategy_orders(exchange: Exchange, symbol: str, logger: logging.Logger, tracker_file_path: str = None):
    """Storniert alle offenen Limit- und Trigger-Orders für die Strategie.

    Optional: wenn `tracker_file_path` übergeben wird und Orders storniert wurden,
    werden die im Tracker gespeicherten SL-/TP-IDs gelöscht, um Inkonsistenzen zu vermeiden.
    """
    cancelled_count = 0
    try:
        # Normale Limit-Orders (könnten Reste sein)
        # Wichtig: Nur Orders für DIESES Symbol stornieren!
        orders = exchange.fetch_open_orders(symbol)
        logger.debug(f"Gefundene offene Limit Orders für {symbol}: {len(orders)}")
        for order in orders:
            try:
                exchange.cancel_order(order['id'], symbol)
                cancelled_count += 1
                logger.info(f"Normale Order {order['id']} ({order['side']} {order['amount']} @ {order.get('price', 'N/A')}) storniert.")
                time.sleep(0.1) # Kleine Pause
            except ccxt.OrderNotFound:
                logger.debug(f"Normale Order {order['id']} war bereits geschlossen/storniert.")
            except Exception as e:
                logger.warning(f"Konnte normale Order {order['id']} nicht stornieren: {e}")

        # Trigger Orders (Entry, TP, SL)
        trigger_orders = exchange.fetch_open_trigger_orders(symbol)
        logger.debug(f"Gefundene offene Trigger Orders für {symbol}: {len(trigger_orders)}")
        for order in trigger_orders:
            # WICHTIG: Trigger-Orders, die als reduceOnly markiert sind (TP/SL),
            # nicht automatisch stornieren — das führt sonst dazu, dass TPs
            # bei jedem Master-Zyklus verschwinden und wieder neu gesetzt werden.
            if order.get('reduceOnly'):
                logger.debug(f"Überspringe reduceOnly Trigger Order {order['id']} ({order.get('side')} {order.get('amount')} @ Trigger {order.get('stopPrice', 'N/A')}).")
                continue
            try:
                exchange.cancel_trigger_order(order['id'], symbol)
                cancelled_count += 1
                logger.info(f"Trigger Order {order['id']} ({order['side']} {order['amount']} @ Trigger {order.get('stopPrice', 'N/A')}) storniert.")
                time.sleep(0.1) # Kleine Pause
            except ccxt.OrderNotFound:
                logger.debug(f"Trigger Order {order['id']} war bereits geschlossen/storniert.")
            except Exception as e:
                logger.warning(f"Konnte Trigger Order {order['id']} nicht stornieren: {e}")

        if cancelled_count > 0:
            logger.info(f"{cancelled_count} offene Order(s) für {symbol} erfolgreich storniert.")
            # Falls ein Tracker-Pfad übergeben wurde, Tracker-Einträge bereinigen
            try:
                if tracker_file_path:
                    tracker_info = read_tracker_file(tracker_file_path)
                    # Entferne bekannte SL/TP IDs, da Orders gelöscht wurden
                    if tracker_info.get("stop_loss_ids") or tracker_info.get("take_profit_ids"):
                        tracker_info["stop_loss_ids"] = []
                        tracker_info["take_profit_ids"] = []
                        update_tracker_file(tracker_file_path, tracker_info)
                        logger.info(f"Tracker ({tracker_file_path}) nach Orderstorno bereinigt.")
            except Exception as e:
                logger.debug(f"Konnte Tracker nach Orderstorno nicht bereinigen: {e}")
        else:
            logger.debug(f"Keine offenen Orders für {symbol} zum Stornieren gefunden.")
        return cancelled_count
    except Exception as e:
        logger.error(f"Fehler beim Stornieren der Orders für {symbol}: {e}", exc_info=True)
        return cancelled_count # Gib bisherige Anzahl zurück

# --- Stop Loss Trigger Check ---

def check_stop_loss_trigger(exchange: Exchange, symbol: str, tracker_file_path: str, logger: logging.Logger):
    """Prüft PRO BAND, ob dessen eigene, feste SL ausgelöst wurde (Multi-Band,
    2026-09-01: jedes Band hat seine eigene SL-Order, siehe place_entry_orders()
    -- nicht mehr EIN SL fuer die gesamte genettete Position). Ein ausgeloestes
    Band-SL schliesst NUR dieses Band; die anderen (falls noch offen) bleiben
    unberuehrt. Erst wenn KEIN Band mehr committed ist, gilt die Position als
    komplett geschlossen (Tracker-Vollreset inkl. last_notified_*)."""
    tracker_info = read_tracker_file(tracker_file_path)
    band_sl_orders = tracker_info.get("band_sl_orders") or {"long": {}, "short": {}}
    all_sl_ids = list(band_sl_orders.get("long", {}).values()) + list(band_sl_orders.get("short", {}).values())
    if not all_sl_ids:
        logger.debug("Keine aktiven per-Band SL-Order-IDs im Tracker gefunden.")
        return False

    logger.debug(f"Prüfe {len(all_sl_ids)} per-Band SL-Order-IDs im Tracker: {all_sl_ids}")

    try:
        open_triggers = exchange.fetch_open_trigger_orders(symbol)
        open_ids = {str(o['id']) for o in open_triggers}

        closed_triggers = []
        params = {'stop': True} if 'bitget' in exchange.exchange.id else {}
        if exchange.exchange.has['fetchClosedOrders']:
            closed_triggers = exchange.exchange.fetchClosedOrders(symbol, limit=15, params=params)
            closed_triggers = [o for o in closed_triggers if o.get('stopPrice') is not None]
        elif exchange.exchange.has['fetchOrders']:
            all_orders = exchange.exchange.fetchOrders(symbol, limit=25, params=params)
            closed_triggers = [o for o in all_orders if o.get('stopPrice') is not None and o['status'] in ['closed', 'canceled']]
        else:
            logger.warning("Weder fetchClosedOrders noch fetchOrders wird unterstützt, um SL-Trigger zu prüfen.")
            return False
        closed_by_id = {str(o.get('id')): o for o in closed_triggers}

        any_triggered = False
        for side_key in ("long", "short"):
            still_open = {}
            for band_str, sl_id in list(band_sl_orders.get(side_key, {}).items()):
                if str(sl_id) in open_ids:
                    still_open[band_str] = sl_id
                    continue
                closed_order = closed_by_id.get(str(sl_id))
                status = closed_order.get('status') if closed_order else None
                if status == 'closed':
                    logger.warning(f"🚨 STOP LOSS für Band {band_str} ({side_key}) von {symbol} ausgelöst! Order ID: {sl_id}")
                    any_triggered = True
                    committed = tracker_info.get("committed_bands") or {"long": [], "short": []}
                    if int(band_str) in committed.get(side_key, []):
                        committed[side_key].remove(int(band_str))
                    tracker_info["committed_bands"] = committed
                else:
                    # Nicht mehr offen, aber auch nicht 'closed' -- z.B. bereits
                    # anderweitig entfernt. Sicherheitshalber einfach fallen lassen.
                    logger.debug(f"Band-SL {sl_id} ({side_key} Band {band_str}) nicht mehr offen (Status={status}).")
            band_sl_orders[side_key] = still_open

        tracker_info["band_sl_orders"] = band_sl_orders

        committed = tracker_info.get("committed_bands") or {"long": [], "short": []}
        fully_flat = not committed.get("long") and not committed.get("short")
        if fully_flat and (any_triggered or not (band_sl_orders.get("long") or band_sl_orders.get("short"))):
            # Keine Baender mehr offen -- Position ist komplett zu, wie ein
            # klassisches "ein SL hat alles geschlossen" (kann durch ein
            # einzelnes Band, das zufaellig das letzte war, oder durch
            # mehrere gleichzeitig ausgeloeste Band-SLs passieren).
            tracker_info["status"] = "stop_loss_triggered" if any_triggered else tracker_info.get("status", "ok_to_trade")
            tracker_info["pending_band_orders"] = {"long": {}, "short": {}}
            if 'last_notified_entry_price' in tracker_info:
                del tracker_info['last_notified_entry_price']
            if 'last_notified_side' in tracker_info:
                del tracker_info['last_notified_side']

        update_tracker_file(tracker_file_path, tracker_info)
        return any_triggered

    except Exception as e:
        logger.error(f"Fehler beim Prüfen geschlossener SL-Orders für {symbol}: {e}", exc_info=True)
        return False


def check_take_profit_trigger(exchange: Exchange, symbol: str, tracker_file_path: str, logger: logging.Logger):
    """Prüft, ob ein von dieser Strategie gesetzter TP ausgelöst wurde."""
    tracker_info = read_tracker_file(tracker_file_path)
    current_tp_ids = tracker_info.get("take_profit_ids", [])
    if not current_tp_ids:
        logger.debug("Keine aktiven TP-Order-IDs im Tracker gefunden.")
        return False

    logger.debug(f"Prüfe {len(current_tp_ids)} TP-Order-IDs im Tracker: {current_tp_ids}")

    try:
        closed_triggers = []
        if exchange.exchange.has['fetchClosedOrders']:
            params = {'stop': True} if 'bitget' in exchange.exchange.id else {}
            closed_triggers = exchange.exchange.fetchClosedOrders(symbol, limit=10, params=params)
            closed_triggers = [o for o in closed_triggers if o.get('stopPrice') is not None]
        elif exchange.exchange.has['fetchOrders']:
            params = {'stop': True} if 'bitget' in exchange.exchange.id else {}
            all_orders = exchange.exchange.fetchOrders(symbol, limit=20, params=params)
            closed_triggers = [o for o in all_orders if o.get('stopPrice') is not None and o['status'] in ['closed', 'canceled']]
        else:
            logger.warning("Weder fetchClosedOrders noch fetchOrders wird unterstützt, um TP-Trigger zu prüfen.")
            return False

        if not closed_triggers:
            logger.debug(f"Keine kürzlich geschlossenen Trigger-Orders für {symbol} gefunden (TP-Prüfung).")
            open_triggers = exchange.fetch_open_trigger_orders(symbol)
            open_trigger_ids = {o['id'] for o in open_triggers}
            still_open_tp_ids = [tp_id for tp_id in current_tp_ids if tp_id in open_trigger_ids]
            if set(still_open_tp_ids) != set(current_tp_ids):
                logger.info("Einige TP-IDs aus dem Tracker sind nicht mehr offen. Aktualisiere Tracker.")
                tracker_info["take_profit_ids"] = still_open_tp_ids
                update_tracker_file(tracker_file_path, tracker_info)
            return False

        triggered_tp_found = False
        for closed_order in closed_triggers:
            closed_id = closed_order['id']
            if closed_id in current_tp_ids:
                if closed_order.get('status') == 'closed':
                    logger.warning(f"✅ TAKE PROFIT wurde für {symbol} ausgelöst! Order ID: {closed_id}")
                    triggered_tp_found = True
                    break

        if triggered_tp_found:
            # TP schließt IMMER die komplette genettete Position (sized auf die
            # aktuelle Gesamtgröße, siehe manage_existing_position()) -- alle
            # Bänder sind wieder frei. Etwaige noch offene per-Band SL-Orders
            # sind jetzt verwaist (keine Position mehr zum Reduzieren) und
            # werden explizit storniert statt sie stehen zu lassen.
            band_sl_orders = tracker_info.get("band_sl_orders") or {"long": {}, "short": {}}
            for side_key in ("long", "short"):
                for band_str, sl_id in band_sl_orders.get(side_key, {}).items():
                    try:
                        exchange.cancel_trigger_order(sl_id, symbol)
                        logger.debug(f"Verwaiste Band-SL {sl_id} ({side_key} Band {band_str}) nach TP storniert.")
                    except Exception as _ce:
                        logger.debug(f"Verwaiste Band-SL {sl_id} bereits entfernt oder Fehler: {_ce}")
            tracker_info.update({
                "status": "take_profit_triggered",
                "take_profit_ids": [],
                "committed_bands": {"long": [], "short": []},
                "pending_band_orders": {"long": {}, "short": {}},
                "band_sl_orders": {"long": {}, "short": {}},
            })
            # Position wurde geschlossen, lösche gemeldete Position aus Tracker
            if 'last_notified_entry_price' in tracker_info:
                del tracker_info['last_notified_entry_price']
            if 'last_notified_side' in tracker_info:
                del tracker_info['last_notified_side']
            update_tracker_file(tracker_file_path, tracker_info)
            return True
        else:
            open_triggers = exchange.fetch_open_trigger_orders(symbol)
            open_trigger_ids = {o['id'] for o in open_triggers}
            still_open_tp_ids = [tp_id for tp_id in current_tp_ids if tp_id in open_trigger_ids]
            if set(still_open_tp_ids) != set(current_tp_ids):
                logger.info("Einige TP-IDs aus dem Tracker sind nicht mehr offen (erneute Prüfung). Aktualisiere Tracker.")
                tracker_info["take_profit_ids"] = still_open_tp_ids
                update_tracker_file(tracker_file_path, tracker_info)
            else:
                logger.debug("Keine ausgelösten TPs gefunden. Alle bekannten TPs sind entweder noch offen oder wurden nicht als 'closed' gemeldet.")
            return False

    except Exception as e:
        logger.error(f"Fehler beim Prüfen geschlossener TP-Orders für {symbol}: {e}", exc_info=True)
        return False

def sync_band_fills(exchange: Exchange, symbol: str, tracker_file_path: str, logger: logging.Logger):
    """
    Multi-Band: gleicht die im Tracker gemerkten "pending" Band-Entry-Order-IDs
    mit der Boerse ab, BEVOR cancel_strategy_orders() sie storniert (das wuerde
    sonst die Unterscheidung "gefuellt vs. nie getriggert" zerstoeren).

    - Order nicht mehr offen UND Status 'closed' -> Band wurde tatsaechlich
      GEFUELLT -> nach committed_bands verschoben (place_entry_orders() eroeffnet
      dieses Band danach nicht mehr erneut).
    - Order nicht mehr offen, aber nicht 'closed' (z.B. von cancel_strategy_orders()
      im letzten Zyklus storniert, weil sie nie getriggert hat) -> einfach aus
      pending_band_orders entfernt, Band bleibt frei fuer die naechste Auswertung.
    """
    tracker_info = read_tracker_file(tracker_file_path)
    pending = tracker_info.get("pending_band_orders") or {"long": {}, "short": {}}
    if not pending.get("long") and not pending.get("short"):
        return

    try:
        open_triggers = exchange.fetch_open_trigger_orders(symbol)
        open_ids = {str(o['id']) for o in open_triggers}
    except Exception as e:
        logger.warning(f"Konnte offene Trigger-Orders für Band-Fill-Abgleich nicht laden: {e}")
        return

    committed = tracker_info.get("committed_bands") or {"long": [], "short": []}
    new_pending = {"long": {}, "short": {}}
    changed = False
    closed_cache = None

    for side_key in ("long", "short"):
        for band_str, order_id in pending.get(side_key, {}).items():
            if str(order_id) in open_ids:
                new_pending[side_key][band_str] = order_id
                continue
            changed = True
            if closed_cache is None:
                closed_cache = []
                try:
                    params = {'stop': True} if 'bitget' in exchange.exchange.id else {}
                    if exchange.exchange.has['fetchClosedOrders']:
                        closed_cache = exchange.exchange.fetchClosedOrders(symbol, limit=15, params=params)
                    elif exchange.exchange.has['fetchOrders']:
                        closed_cache = exchange.exchange.fetchOrders(symbol, limit=25, params=params)
                except Exception as e:
                    logger.debug(f"Konnte geschlossene Orders für Band-Fill-Abgleich nicht laden: {e}")
            status = None
            for o in closed_cache or []:
                if str(o.get('id')) == str(order_id):
                    status = o.get('status')
                    break
            band_num = int(band_str)
            if status == 'closed':
                if band_num not in committed.get(side_key, []):
                    committed.setdefault(side_key, []).append(band_num)
                logger.info(f"✅ Band {band_num} ({side_key}) für {symbol} wurde GEFÜLLT (Order {order_id}) -- als committed markiert.")
            else:
                logger.debug(f"Band {band_num} ({side_key}) Entry-Order {order_id} nicht mehr offen (Status={status}) -- vermutlich storniert, Band wird wieder frei.")

    if changed:
        tracker_info["pending_band_orders"] = new_pending
        tracker_info["committed_bands"] = committed
        update_tracker_file(tracker_file_path, tracker_info)


# --- Positions-Management ---

def manage_existing_position(exchange: Exchange, position: dict, band_prices: dict, params: dict, tracker_file_path: str, logger: logging.Logger):
    """Verwaltet eine bestehende Position: aktualisiert NUR den gebündelten TP
    (aktuelle MA, jeden Zyklus neu, sized auf die aktuelle genettete Positionsgröße).

    SL wird hier NICHT mehr gesetzt/angefasst -- jedes Band hat seine eigene,
    feste SL-Order (siehe place_entry_orders()), die unveraendert bestehen
    bleibt bis sie feuert (sync_band_sl_fills()) oder der gebündelte TP die
    komplette Position schließt (check_take_profit_trigger()). Das matcht
    backtester.py, wo jede Band-Position ihre eigene feste SL hat und nur
    der TP fuer alle gemeinsam dynamisch ist (2026-09-01, "das muss sauber
    sein" -- vorher wurde hier faelschlich EINE SL fuer die gesamte genettete
    Position neu berechnet, was Band-1-SL-Level ignorierte sobald Band 2/3
    zu einem guenstigeren Average-Entry dazukamen).
    """
    symbol = params['market']['symbol']
    pos_side = position['side']
    logger.info(f"Verwalte bestehende {pos_side}-Position für {symbol} (Größe: {position.get('contracts', 'N/A')}) -- aktualisiere gebündelten TP.")

    amount_contracts = position['contracts']
    try:
        amount_contracts_float = float(amount_contracts)
        if amount_contracts_float == 0:
             logger.warning("Positionsgröße ist 0, kann TP nicht setzen.")
             return
    except (ValueError, TypeError) as e:
        logger.error(f"Konnte Positionsgröße ('{amount_contracts}') nicht in Float umwandeln: {e}")
        return

    # NUR die zuvor getrackten TP-Order(s) stornieren -- die per-Band SL-Orders
    # (ebenfalls reduceOnly) bleiben unangetastet.
    tracker_info = read_tracker_file(tracker_file_path)
    old_tp_ids = tracker_info.get("take_profit_ids", [])
    if old_tp_ids:
        for _tp_id in old_tp_ids:
            try:
                exchange.cancel_trigger_order(_tp_id, symbol)
                logger.debug(f"Alter TP storniert: {_tp_id}")
            except Exception as _ce:
                logger.debug(f"TP {_tp_id} bereits entfernt oder Fehler: {_ce}")

    new_tp_ids = []
    try:
        tp_price = band_prices.get('average')
        if tp_price is None or pd.isna(tp_price) or tp_price <= 0:
            logger.error("Ungültiger Average-Preis für TP. Überspringe TP-Platzierung.")
        else:
            tp_side = 'sell' if pos_side == 'long' else 'buy'

            use_native_tp = params.get('risk', {}).get('use_native_trailing_tp', False)
            tp_callback_rate = params.get('risk', {}).get('tp_trailing_callback_rate_pct', 0.5) / 100.0
            tp_activation_delta = params.get('strategy', {}).get('tp_activation_delta_pct', 0.5) / 100.0
            avg_entry_price_str = position.get('entryPrice', position.get('info', {}).get('avgOpenPrice'))
            if avg_entry_price_str is None:
                avg_entry_price_str = position.get('info', {}).get('openPriceAvg')
            try:
                avg_entry_price = float(avg_entry_price_str) if avg_entry_price_str is not None else tp_price
            except (ValueError, TypeError):
                avg_entry_price = tp_price

            if use_native_tp:
                if pos_side == 'long':
                    activation_price = max(tp_price, avg_entry_price * (1 + tp_activation_delta))
                else:
                    activation_price = min(tp_price, avg_entry_price * (1 - tp_activation_delta))
                try:
                    resp = exchange.place_trailing_stop_order(
                        symbol=symbol, side=tp_side, amount=amount_contracts_float,
                        activation_price=activation_price, callback_rate_decimal=tp_callback_rate,
                        params={'reduceOnly': True}
                    )
                    tp_id = None
                    if isinstance(resp, dict):
                        if 'data' in resp and isinstance(resp['data'], dict):
                            for key in ('orderId', 'planId', 'id'):
                                if key in resp['data']:
                                    tp_id = resp['data'][key]
                                    break
                        for key in ('orderId', 'planId', 'id'):
                            if not tp_id and key in resp:
                                tp_id = resp[key]
                    if tp_id:
                        new_tp_ids.append(tp_id)
                    logger.info(f"Neuen native Trailing-TP für {pos_side} gesetzt (activation={activation_price:.4f}, callback={tp_callback_rate*100:.2f}%). RespID={tp_id}")
                except Exception as e:
                    logger.warning(f"Native Trailing-TP nicht möglich, fallback auf Trigger-TP: {e}")
                    tp_order = exchange.place_trigger_market_order(symbol, tp_side, amount_contracts_float, tp_price, reduce=True)
                    if tp_order and 'id' in tp_order:
                        new_tp_ids.append(tp_order['id'])
                    logger.info(f"Neuen TP für {pos_side} @ {tp_price:.4f} gesetzt (Größe: {amount_contracts_float:.4f}).")
            else:
                tp_order = exchange.place_trigger_market_order(symbol, tp_side, amount_contracts_float, tp_price, reduce=True)
                if tp_order and 'id' in tp_order:
                    new_tp_ids.append(tp_order['id'])
                logger.info(f"Neuen TP für {pos_side} @ {tp_price:.4f} gesetzt (Größe: {amount_contracts_float:.4f}).")
            time.sleep(0.1)

    except ccxt.InsufficientFunds as e:
         logger.error(f"Nicht genügend Guthaben zum Setzen von TP (sollte bei reduceOnly nicht passieren): {e}")
    except ccxt.ExchangeError as e:
         logger.warning(f"Börsenfehler beim Setzen von TP für {symbol}: {e}")
    except Exception as e:
        logger.error(f"Fehler beim Setzen von neuem TP für {symbol}: {e}", exc_info=True)

    tracker_info = read_tracker_file(tracker_file_path)
    tracker_info["take_profit_ids"] = new_tp_ids
    update_tracker_file(tracker_file_path, tracker_info)


# --- Entry Order Platzierung ---

def place_entry_orders(exchange: Exchange, band_prices: dict, params: dict, balance: float, tracker_file_path: str, telegram_config: dict, logger: logging.Logger, df: pd.DataFrame = None,
                       restrict_side: str = None, committed_bands: dict = None):
    """Platziert die gestaffelten Entry- und (pro Band eigene, feste) SL-Orders
    basierend auf Risiko. Der TP wird NICHT hier gesetzt -- er ist fuer alle
    Baender identisch (aktuelle MA) und wird deshalb gebuendelt, sized auf die
    jeweils aktuelle genettete Positionsgroesse, von manage_existing_position()
    verwaltet, sobald eine Position existiert.

    Multi-Band: pro Zyklus werden ALLE Baender geprueft (kein break mehr nach dem
    ersten Treffer) -- matcht backtester.py, das ebenfalls jede Kerze alle
    qualifizierenden Baender oeffnet.

    restrict_side: 'long'/'short' beschraenkt auf eine Seite (wird gesetzt, wenn
        bereits eine Position offen ist -- verhindert, dass zusaetzlich die
        Gegenseite eroeffnet wird, was in Bitgets One-Way-Modus die bestehende
        Position teilweise gegenrechnen wuerde).
    committed_bands: {'long': [1,2,...], 'short': [...]} -- Baender, die laut
        Tracker bereits GEFUELLT sind (nicht mehr nur pending) und daher nicht
        erneut eroeffnet werden duerfen.
    """
    if committed_bands is None:
        committed_bands = {'long': [], 'short': []}
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    risk_params = params['risk']
    strategy_params = params['strategy']
    behavior_params = params['behavior'].copy()  # Copy um zu modifizieren
    account_name = exchange.account.get('name', 'Standard-Account')

    logger.info(f"Platziere neue Entry-Orders für {symbol} (Risikobasierte Größe). Aktueller Saldo: {balance:.2f} USDT")
    
    # Marktregime prüfen
    regime = band_prices.get('regime', 'UNCERTAIN')
    trend_direction = band_prices.get('trend_direction', 'NEUTRAL')
    supertrend_direction = band_prices.get('supertrend_direction', 'NEUTRAL')
    adx = band_prices.get('adx')
    price_distance_pct = band_prices.get('price_distance_pct')
    logger.info(f"📊 Marktregime: {regime} | Trend: {trend_direction} | Supertrend: {supertrend_direction} | ADX: {adx} | price_distance_pct: {price_distance_pct}")

    # NEU: Bei STRONG_TREND sofort abbrechen, keine Trigger platzieren
    if regime == "STRONG_TREND":
        logger.warning(f"⚠️ STRONG_TREND erkannt - KEINE neuen Trigger/Entries werden platziert! (ADX={adx})")
        return

    # Trend-Bias anwenden (asymmetrisches Trading - MIT dem Trend handeln)
    # Im Uptrend bei Pullbacks kaufen, im Downtrend bei Rallies shorten
    if trend_direction == "UPTREND":
        # Im Uptrend: Nur Longs erlaubt (Shorts deaktiviert)
        behavior_params['use_shorts'] = False
        logger.warning(f"⬆️ UPTREND erkannt - Short-Entries DEAKTIVIERT (Trading MIT dem Trend)")
    elif trend_direction == "DOWNTREND":
        # Im Downtrend: Nur Shorts erlaubt (Longs deaktiviert)
        behavior_params['use_longs'] = False
        logger.warning(f"⬇️ DOWNTREND erkannt - Long-Entries DEAKTIVIERT (Trading MIT dem Trend)")

    # Parameter holen
    leverage = risk_params['leverage']
    risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Risiko pro Layer aus Config

    # SL-Modus (Priorität: sl_ratio → ATR-mult → fixer %)
    _envelopes_cfg = strategy_params.get('envelopes', [0.03, 0.05, 0.08])
    if 'sl_to_env1_ratio' in risk_params:
        _sl_mode    = 'ratio'
        _sl_ratio   = risk_params['sl_to_env1_ratio']
        stop_loss_pct_param = None; _current_atr = 0.0; _atr_sl_mult = 0.0; _min_sl_pct = 0.0
    elif 'stop_loss_atr_multiplier' in risk_params:
        _sl_mode       = 'atr'
        _atr_sl_mult   = risk_params['stop_loss_atr_multiplier']
        _atr_sl_period = risk_params.get('stop_loss_atr_period', 14)
        _min_sl_pct    = risk_params.get('min_stop_loss_pct', 0.5) / 100.0
        if df is not None and len(df) >= _atr_sl_period:
            _atr_series  = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=_atr_sl_period)
            _current_atr = float(_atr_series.iloc[-1]) if pd.notna(_atr_series.iloc[-1]) else 0.0
        else:
            _current_atr = 0.0
        stop_loss_pct_param = None; _sl_ratio = None
    else:
        _sl_mode = 'fixed'
        stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0
        _current_atr = 0.0; _atr_sl_mult = 0.0; _min_sl_pct = 0.0; _sl_ratio = None
        if regime == "TREND" or regime == "STRONG_TREND":
            stop_loss_pct_param *= 1.5
            logger.info(f"📈 Trend-Markt: Stop-Loss erweitert auf {stop_loss_pct_param*100:.2f}%")
    num_envelopes = len(strategy_params['envelopes'])
    min_amount_tradable = exchange.fetch_min_amount_tradable(symbol)
    trigger_delta_pct_cfg = strategy_params.get('trigger_price_delta_pct', 0.05) / 100.0

    # Aktueller LIVE-Preis fuer SL-Sofort-Pruefung (vermeidet sofortigen SL-Trigger).
    # Vorher wurde hier df['close'].iloc[-1] verwendet -- der Schlusskurs der letzten
    # ABGESCHLOSSENEN Kerze, bis zu einem ganzen Timeframe (z.B. 30 Min) alt. Beobachtet
    # 2026-09-01: Preis fiel zwischen Kerzen-Close und Zyklus-Auswertung so weit, dass er
    # den SL-Level bereits unterschritten hatte, WAEHREND der stale Kerzen-Close-Check noch
    # "ueber SL" zeigte -- die Entry-Order (samt SL) wurde trotzdem platziert. Fix: echten
    # Live-Preis per Ticker abfragen statt der potenziell veralteten Kerze.
    current_close = None
    try:
        _ticker = exchange.fetch_ticker(symbol)
        if _ticker and _ticker.get('last'):
            current_close = float(_ticker['last'])
    except Exception as e:
        logger.warning(f"Konnte Live-Ticker für Sofort-SL-Pruefung nicht abrufen ({e}), nutze Kerzen-Close als Fallback.")
    if current_close is None and df is not None and not df.empty:
        current_close = float(df['close'].iloc[-1])

    # *** RISIKOBASIS: echter, aktueller Kontostand (Compounding, konsistent mit Backtester) ***
    # User-Entscheidung 2026-08-26: Positionsgroesse soll vom TATSAECHLICHEN
    # Gesamtkapital abhaengen, kein statischer Referenzwert als kuenstliche
    # Bremse mehr. `balance` ist der echte, gerade erst via fetch_balance_usdt()
    # abgerufene Kontostand (siehe Aufrufer). Fallback-Reihenfolge:
    # 1) initial_capital_live in Config (expliziter Override, falls gesetzt),
    # 2) aktueller Kontostand, 3) nur falls beides fehlt/ungueltig: statischer
    # Wert aus settings.json als letzte Notbremse (z.B. API-Fehler lieferte 0).
    risk_base_capital = params.get('initial_capital_live') or balance
    if not risk_base_capital:
        try:
            settings_path = os.path.join(PROJECT_ROOT, 'settings.json')
            with open(settings_path, 'r') as _f:
                _settings = json.load(_f)
            risk_base_capital = _settings.get('optimization_settings', {}).get('start_capital', 10)
        except Exception:
            risk_base_capital = 10
    logger.info(f"Risikoberechnung basiert auf: {risk_base_capital:.2f} USDT")

    new_sl_ids = {'long': {}, 'short': {}}
    new_pending_entries = {'long': {}, 'short': {}}

    # --- Long Orders ---
    if behavior_params.get('use_longs', True) and restrict_side in (None, 'long'):
        side = 'buy'
        logger.info(f"Prüfe Long Entry Bands: {band_prices.get('long', [])}")
        for i, entry_limit_price in enumerate(band_prices.get('long', [])):
            if (i + 1) in committed_bands.get('long', []):
                logger.debug(f"Long Band {i+1} bereits gefuellt (Tracker). Ueberspringe.")
                continue
            if entry_limit_price is None or pd.isna(entry_limit_price) or entry_limit_price <= 0:
                logger.warning(f"Ungültiger Long-Entry-Preis ({entry_limit_price}) für Band {i+1}. Überspringe.")
                continue

            try:
                # Close-Confirmation: letzte abgeschlossene Kerze muss unterhalb des Bands geschlossen haben
                if df is not None and not df.empty:
                    last_close = float(df['close'].iloc[-1])
                    low_band_col = f'band_low_{i+1}'
                    last_band_low = float(df[low_band_col].iloc[-1]) if low_band_col in df.columns else entry_limit_price
                    if last_close > last_band_low:
                        logger.info(f"Long Layer {i+1}: Kein Close-Confirmation (Close {last_close:.6g} > Band {last_band_low:.6g}). Überspringe.")
                        continue

                # 1. Risiko in USD berechnen (basierend auf gewählter Basis)
                risk_amount_usd = risk_base_capital * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0:
                    logger.warning(f"Risk amount <= 0 ({risk_amount_usd:.2f}) für Layer {i+1}. Skipping.")
                    continue

                # 2. SL-Preis und Distanz berechnen
                entry_price_for_calc = entry_limit_price
                if _sl_mode == 'ratio':
                    env_pct = _envelopes_cfg[i] if i < len(_envelopes_cfg) else _envelopes_cfg[0]
                    sl_pct_dyn = env_pct * _sl_ratio
                    if regime in ("TREND", "STRONG_TREND"):
                        sl_pct_dyn *= 1.5
                    sl_price = entry_price_for_calc * (1 - sl_pct_dyn)
                elif _sl_mode == 'atr':
                    if _current_atr > 0 and entry_price_for_calc > 0:
                        sl_pct_dyn = max(_current_atr * _atr_sl_mult / entry_price_for_calc, _min_sl_pct)
                    else:
                        sl_pct_dyn = _min_sl_pct
                    if regime in ("TREND", "STRONG_TREND"):
                        sl_pct_dyn *= 1.5
                    sl_price = entry_price_for_calc * (1 - sl_pct_dyn)
                else:
                    sl_price = entry_price_for_calc * (1 - stop_loss_pct_param)
                if sl_price <= 0:
                     logger.warning(f"Negativer oder Null SL-Preis ({sl_price:.4f}) berechnet für Entry {entry_price_for_calc:.4f}. Überspringe Layer {i+1}.")
                     continue
                # Preis bereits unter SL → Entry würde sofort gestoppt (immediate SL-Trigger)
                if current_close is not None and current_close < sl_price:
                    logger.warning(f"⚠️ Aktueller Preis {current_close:.4f} < SL {sl_price:.4f} für Long Layer {i+1} → überspringe (sofortiger SL vermieden).")
                    continue
                sl_distance_price = abs(entry_price_for_calc - sl_price)
                if sl_distance_price <= 0:
                    logger.warning(f"SL distance <= 0 für entry {entry_price_for_calc:.4f}. Skipping Layer {i+1}.")
                    continue

                # 3. Positionsgröße (amount_coins) berechnen
                amount_coins = risk_amount_usd / sl_distance_price

                # 4. Mindestmenge prüfen
                if amount_coins < min_amount_tradable:
                    logger.warning(f"Berechnete Long-Menge {amount_coins:.8f} für Layer {i+1} liegt unter Minimum {min_amount_tradable:.8f}. Überspringe.")
                    continue

                # 4b. Mindest-Notional-Wert prüfen (Bitget: min. 5 USDT)
                MIN_NOTIONAL_USDT = 5.0
                notional_value = amount_coins * entry_price_for_calc
                if notional_value < MIN_NOTIONAL_USDT:
                    logger.warning(f"Notional-Wert {notional_value:.2f} USDT für Long Layer {i+1} unter Bitget-Minimum {MIN_NOTIONAL_USDT} USDT (Kapital zu klein für diesen SL-Abstand). Überspringe.")
                    continue

                # 5. Benötigte Margin (nur zur Info)
                margin_required = (amount_coins * entry_price_for_calc) / leverage
                logger.debug(f"Long Layer {i+1}: Risk={risk_amount_usd:.2f}$, Size={amount_coins:.8f}, MarginReq={margin_required:.2f}$ (Verfügbar ca.: {balance:.2f})")

                # KORRIGIERT: Trigger UNTER dem Limit-Preis für Long
                # (Entry erst wenn Preis tief genug gefallen ist)
                entry_trigger_price = entry_limit_price * (1 - trigger_delta_pct_cfg)


                # Eigene, FESTE SL fuer GENAU dieses Band platzieren (reduceOnly,
                # auf amount_coins dieses Bands begrenzt) -- bleibt unveraendert
                # bestehen bis sie feuert oder die Position komplett schliesst,
                # wird NICHT von manage_existing_position() angefasst (das
                # verwaltet nur noch den gebuendelten TP). Matcht den Backtester,
                # der pro Band ebenfalls eine feste, bei Entry berechnete SL nutzt
                # (backtester.py: pos['sl_price']), statt einer bei jedem Zyklus
                # neu berechneten SL fuer die gesamte genettete Position.
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='sell', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Long Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids['long'][i + 1] = sl_order['id']
                time.sleep(0.1)

                # Dann Entry Order (Trigger Limit)
                entry_order = exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                if entry_order and 'id' in entry_order:
                    new_pending_entries['long'][i + 1] = entry_order['id']
                logger.info(f"✅ Long Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")
                time.sleep(0.1)

                # Telegram (kein TP-Preis mehr hier -- der gebuendelte TP wird
                # von manage_existing_position() gesetzt, sobald die Position
                # existiert, und deckt dann alle offenen Baender gemeinsam ab)
                if sl_price and sl_price > 0:
                    sl_pct_msg = abs(entry_limit_price - sl_price) / entry_limit_price * 100 if entry_limit_price > 0 else 0
                    regime_msg = band_prices.get('regime', '')
                    trend_msg  = band_prices.get('trend_direction', '')
                    msg = (
                        f"LONG ENTRY-ORDER PLATZIERT\n\n"
                        f"Symbol: {symbol} ({timeframe})\n"
                        f"Entry: {entry_limit_price:.6g} USDT (Band {i+1})\n"
                        f"SL: {sl_price:.6g} USDT (-{sl_pct_msg:.2f}%, fest für dieses Band)\n"
                        f"TP: folgt gebündelt via MA sobald gefüllt\n"
                        f"Hebel: {leverage}x\n"
                        f"Regime: {regime_msg} | Trend: {trend_msg}"
                    )
                    send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)
                    if df is not None:
                        _send_ltbbot_chart(df, band_prices, 'buy', entry_limit_price,
                                           sl_price, band_prices.get('average'), symbol, timeframe,
                                           telegram_config, logger)
                # Kein break mehr: alle qualifizierenden Baender pruefen (Multi-Band,
                # 2026-09-01) -- Bitget nettet ohnehin zu einer Position pro Symbol.

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Long-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders für DIESE SEITE.")
                break
            except ccxt.ExchangeError as e:
                 logger.error(f"Börsenfehler beim Platzieren der Long-Order-Gruppe {i+1}: {e}")
                 # Hier könnte man spezifische Fehler behandeln, z.B. Preis zu weit weg etc.
            except Exception as e:
                logger.error(f"Allg. Fehler beim Platzieren der Long-Order-Gruppe {i+1}: {e}", exc_info=True)
                # Nicht abbrechen, versuche nächsten Layer

    # --- Short Orders ---
    if behavior_params.get('use_shorts', True) and restrict_side in (None, 'short'):
        side = 'sell'
        logger.info(f"Prüfe Short Entry Bands: {band_prices.get('short', [])}")
        for i, entry_limit_price in enumerate(band_prices.get('short', [])):
            if (i + 1) in committed_bands.get('short', []):
                logger.debug(f"Short Band {i+1} bereits gefuellt (Tracker). Ueberspringe.")
                continue
            if entry_limit_price is None or pd.isna(entry_limit_price) or entry_limit_price <= 0:
                logger.warning(f"Ungültiger Short-Entry-Preis ({entry_limit_price}) für Band {i+1}. Überspringe.")
                continue

            try:
                # Close-Confirmation: letzte abgeschlossene Kerze muss oberhalb des Bands geschlossen haben
                if df is not None and not df.empty:
                    last_close = float(df['close'].iloc[-1])
                    high_band_col = f'band_high_{i+1}'
                    last_band_high = float(df[high_band_col].iloc[-1]) if high_band_col in df.columns else entry_limit_price
                    if last_close < last_band_high:
                        logger.info(f"Short Layer {i+1}: Kein Close-Confirmation (Close {last_close:.6g} < Band {last_band_high:.6g}). Überspringe.")
                        continue

                # 1. Risiko in USD berechnen (basierend auf gewählter Basis)
                risk_amount_usd = risk_base_capital * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0: continue

                # 2. SL-Preis und Distanz berechnen
                entry_price_for_calc = entry_limit_price
                if _sl_mode == 'ratio':
                    env_pct = _envelopes_cfg[i] if i < len(_envelopes_cfg) else _envelopes_cfg[0]
                    sl_pct_dyn = env_pct * _sl_ratio
                    if regime in ("TREND", "STRONG_TREND"):
                        sl_pct_dyn *= 1.5
                    sl_price = entry_price_for_calc * (1 + sl_pct_dyn)
                elif _sl_mode == 'atr':
                    if _current_atr > 0 and entry_price_for_calc > 0:
                        sl_pct_dyn = max(_current_atr * _atr_sl_mult / entry_price_for_calc, _min_sl_pct)
                    else:
                        sl_pct_dyn = _min_sl_pct
                    if regime in ("TREND", "STRONG_TREND"):
                        sl_pct_dyn *= 1.5
                    sl_price = entry_price_for_calc * (1 + sl_pct_dyn)
                else:
                    sl_price = entry_price_for_calc * (1 + stop_loss_pct_param)
                if sl_price <= 0: continue
                # Preis bereits über SL → Entry würde sofort gestoppt (immediate SL-Trigger)
                if current_close is not None and current_close > sl_price:
                    logger.warning(f"⚠️ Aktueller Preis {current_close:.4f} > SL {sl_price:.4f} für Short Layer {i+1} → überspringe (sofortiger SL vermieden).")
                    continue
                sl_distance_price = abs(entry_price_for_calc - sl_price)
                if sl_distance_price <= 0: continue

                # 3. Positionsgröße (amount_coins) berechnen
                amount_coins = risk_amount_usd / sl_distance_price

                # 4. Mindestmenge prüfen
                if amount_coins < min_amount_tradable:
                    logger.warning(f"Berechnete Short-Menge {amount_coins:.8f} für Layer {i+1} liegt unter Minimum {min_amount_tradable:.8f}. Überspringe.")
                    continue

                # 4b. Mindest-Notional-Wert prüfen (Bitget: min. 5 USDT)
                MIN_NOTIONAL_USDT = 5.0
                notional_value = amount_coins * entry_price_for_calc
                if notional_value < MIN_NOTIONAL_USDT:
                    logger.warning(f"Notional-Wert {notional_value:.2f} USDT für Short Layer {i+1} unter Bitget-Minimum {MIN_NOTIONAL_USDT} USDT (Kapital zu klein für diesen SL-Abstand). Überspringe.")
                    continue

                # 5. Benötigte Margin (nur zur Info)
                margin_required = (amount_coins * entry_price_for_calc) / leverage
                logger.debug(f"Short Layer {i+1}: Risk={risk_amount_usd:.2f}$, Size={amount_coins:.8f}, MarginReq={margin_required:.2f}$ (Verfügbar ca.: {balance:.2f})")

                # KORRIGIERT: Trigger ÜBER dem Limit-Preis für Short
                # (Entry erst wenn Preis hoch genug gestiegen ist)
                entry_trigger_price = entry_limit_price * (1 + trigger_delta_pct_cfg)


                # Eigene, FESTE SL fuer GENAU dieses Band (siehe Long-Block-Kommentar
                # weiter oben -- matcht backtester.py's pos['sl_price']-Modell).
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='buy', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Short Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids['short'][i + 1] = sl_order['id']
                time.sleep(0.1)

                # Dann Entry Order (Trigger Limit)
                entry_order = exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                if entry_order and 'id' in entry_order:
                    new_pending_entries['short'][i + 1] = entry_order['id']
                logger.info(f"✅ Short Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")
                time.sleep(0.1)

                # Telegram (kein TP-Preis mehr hier -- siehe Long-Block-Kommentar)
                if sl_price and sl_price > 0:
                    sl_pct_msg = abs(sl_price - entry_limit_price) / entry_limit_price * 100 if entry_limit_price > 0 else 0
                    regime_msg = band_prices.get('regime', '')
                    trend_msg  = band_prices.get('trend_direction', '')
                    msg = (
                        f"SHORT ENTRY-ORDER PLATZIERT\n\n"
                        f"Symbol: {symbol} ({timeframe})\n"
                        f"Entry: {entry_limit_price:.6g} USDT (Band {i+1})\n"
                        f"SL: {sl_price:.6g} USDT (+{sl_pct_msg:.2f}%, fest für dieses Band)\n"
                        f"TP: folgt gebündelt via MA sobald gefüllt\n"
                        f"Hebel: {leverage}x\n"
                        f"Regime: {regime_msg} | Trend: {trend_msg}"
                    )
                    send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)
                    if df is not None:
                        _send_ltbbot_chart(df, band_prices, 'sell', entry_limit_price,
                                           sl_price, band_prices.get('average'), symbol, timeframe,
                                           telegram_config, logger)
                # Kein break mehr: alle qualifizierenden Baender pruefen (Multi-Band,
                # 2026-09-01) -- Bitget nettet ohnehin zu einer Position pro Symbol.

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Short-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders für DIESE SEITE.")
                break
            except ccxt.ExchangeError as e:
                 logger.error(f"Börsenfehler beim Platzieren der Short-Order-Gruppe {i+1}: {e}")
            except Exception as e:
                logger.error(f"Allg. Fehler beim Platzieren der Short-Order-Gruppe {i+1}: {e}", exc_info=True)


    # Tracker aktualisieren: neue per-Band SL-IDs + neue pending Band-Entry-IDs
    # (beide zusammenfuehren, nicht ueberschreiben -- andere Baender/Seiten
    # koennen bereits eigene Eintraege haben, die noch nicht durch
    # sync_band_fills()/check_stop_loss_trigger() aufgeloest wurden).
    any_new_sl = bool(new_sl_ids['long']) or bool(new_sl_ids['short'])
    any_new_pending = bool(new_pending_entries['long']) or bool(new_pending_entries['short'])
    if any_new_sl or any_new_pending:
        tracker_info = read_tracker_file(tracker_file_path)
        band_sl_orders = tracker_info.get("band_sl_orders") or {"long": {}, "short": {}}
        pending = tracker_info.get("pending_band_orders") or {"long": {}, "short": {}}
        for side_key in ("long", "short"):
            band_sl_orders.setdefault(side_key, {})
            pending.setdefault(side_key, {})
            for band_num, order_id in new_sl_ids[side_key].items():
                band_sl_orders[side_key][str(band_num)] = order_id
            for band_num, order_id in new_pending_entries[side_key].items():
                pending[side_key][str(band_num)] = order_id
        tracker_info["band_sl_orders"] = band_sl_orders
        tracker_info["pending_band_orders"] = pending
        # WICHTIG: Wenn neue Entries platziert werden, ist der Cooldown definitiv vorbei
        tracker_info["status"] = "ok_to_trade"
        tracker_info["last_side"] = None
        update_tracker_file(tracker_file_path, tracker_info)
        if any_new_sl:
            logger.info(f"Tracker mit neuen per-Band SL-IDs aktualisiert: {new_sl_ids}")
        if any_new_pending:
            logger.info(f"Tracker mit pending Band-Entries aktualisiert: {new_pending_entries}")
    elif not any(p is not None and not pd.isna(p) for p in band_prices.get('long', [])) and \
         not any(p is not None and not pd.isna(p) for p in band_prices.get('short', [])): # Keine gültigen Preise gefunden
           logger.info("Keine gültigen Entry-Preise gefunden, keine Orders platziert.")
    else:
           logger.info("Keine Entry-Orders platziert (ggf. Menge zu klein, Margin, Max Pos Size oder Fehler).")

# --- Haupt-Zyklus ---

def full_trade_cycle(exchange: Exchange, params: dict, telegram_config: dict, logger: logging.Logger):
    """Der Haupt-Handelszyklus für eine einzelne Envelope-Strategie."""
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    tracker_file_path = get_tracker_file_path(symbol, timeframe)
    account_name = exchange.account.get('name', 'Standard-Account')
    logger.info(f"===== Starte Handelszyklus für {symbol} ({timeframe}) auf '{account_name}' =====")

    try:
        # --- 1. Daten holen und Indikatoren berechnen ---
        # Brauchen genug Daten für den längsten Indikator (average_period) + etwas Puffer
        required_candles = params['strategy'].get('average_period', 20) + 50 # Puffer erhöht
        data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=required_candles)
        data = drop_incomplete_last_candle(data)
        if data.empty or len(data) < params['strategy'].get('average_period', 1):
            logger.warning(f"Nicht genügend Daten für {symbol} ({timeframe}) erhalten ({len(data)} Kerzen). Überspringe Zyklus.")
            return

        data_with_indicators, band_prices = calculate_indicators_and_signals(data, params)

        # Prüfen ob band_prices und der average gültig sind
        current_average = band_prices.get('average')
        if current_average is None or pd.isna(current_average):
            logger.warning(f"Konnte Indikatoren (Average) nicht berechnen für {symbol}. Überspringe.")
            return

        last_price = data['close'].iloc[-1]
        logger.info(f"Aktueller Status: Preis={last_price:.4f}, Average={current_average:.4f}")
        # Debug Log für Bandpreise
        logger.debug(f"Berechnete Bandpreise: Long={band_prices.get('long')}, Short={band_prices.get('short')}")
        
        # Marktregime-Check
        regime = band_prices.get('regime', 'UNCERTAIN')
        trade_allowed = band_prices.get('trade_allowed', True)
        trend_direction = band_prices.get('trend_direction', 'NEUTRAL')
        supertrend_direction = band_prices.get('supertrend_direction', 'NEUTRAL')
        adx = band_prices.get('adx')
        price_distance_pct = band_prices.get('price_distance_pct')

        logger.info(f"📊 Marktregime: {regime} | Trend: {trend_direction} | Supertrend: {supertrend_direction} | Trading: {'✅' if trade_allowed else '❌'} | ADX: {adx:.2f} | Abstand: {price_distance_pct:.2f}%")

        # Bei starkem Trend: Nur bestehende Positionen verwalten
        if regime == "STRONG_TREND" and not trade_allowed:
            logger.warning(f"⚠️ STARKER TREND erkannt - Keine neuen Entries erlaubt! (ADX={adx})")
            cancel_strategy_orders(exchange, symbol, logger)
            # Prüfe ob Position existiert
            position_list = exchange.fetch_open_positions(symbol)
            if position_list:
                logger.info("Position vorhanden - verwalte TP/SL")
                manage_existing_position(exchange, position_list[0], band_prices, params, tracker_file_path, logger)
            return  # Beende Zyklus früh


        # --- 2. Prüfen, ob TP/SL ausgelöst wurden SEIT dem letzten Lauf ---
        check_take_profit_trigger(exchange, symbol, tracker_file_path, logger)
        check_stop_loss_trigger(exchange, symbol, tracker_file_path, logger)

        # --- 2b. Multi-Band: pending Entry-Orders mit der Börse abgleichen (gefüllt
        # vs. storniert), BEVOR cancel_strategy_orders() sie gleich wegwirft ---
        sync_band_fills(exchange, symbol, tracker_file_path, logger)

        # --- 3. Alle alten Orders der Strategie stornieren (wichtig!) ---
        cancel_strategy_orders(exchange, symbol, logger)

        # --- 4. Offene Position prüfen und verwalten ---
        position_list = exchange.fetch_open_positions(symbol)
        position = position_list[0] if position_list else None

        tracker_info = read_tracker_file(tracker_file_path)
        committed_bands = tracker_info.get("committed_bands") or {"long": [], "short": []}

        if position:
            manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger)
            logger.info(f"Position für {symbol} ist offen ({position['side']} {position['contracts']}). TP aktualisiert.")
            check_and_notify_new_position(exchange, position, params, tracker_file_path, telegram_config, logger)

            # Multi-Band: auf derselben Seite koennen weitere, noch nicht committete
            # Baender dazukommen (Bitget nettet zu EINER Position; jedes neue Band
            # bekommt seine eigene feste SL, der gebuendelte TP wird im naechsten
            # Zyklus von manage_existing_position() automatisch auf die dann
            # groessere Positionsgroesse angepasst).
            current_balance = exchange.fetch_balance_usdt()
            place_entry_orders(exchange, band_prices, params, current_balance, tracker_file_path, telegram_config, logger,
                               df=data_with_indicators, restrict_side=position['side'],
                               committed_bands=committed_bands)

        else:
              logger.info(f"Keine offene Position für {symbol}.")
              current_balance = exchange.fetch_balance_usdt()
              if current_balance <= 1:
                  logger.error(f"Guthaben ({current_balance:.2f} USDT) zu gering zum Platzieren von Entry-Orders.")
                  message = f"📉 *Guthaben zu gering* bei {account_name} ({symbol}): {current_balance:.2f} USDT."
                  send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
                  return

              try:
                  risk_params = params['risk']
                  exchange.set_margin_mode(symbol, margin_mode=risk_params['margin_mode'])
                  time.sleep(0.5)
                  exchange.set_leverage(symbol, margin_mode=risk_params['margin_mode'], leverage=risk_params['leverage'])
                  time.sleep(0.5)
              except Exception as e:
                  logger.warning(f"Konnte Margin Mode/Leverage nicht setzen (evtl. schon korrekt?): {e}")

              place_entry_orders(exchange, band_prices, params, current_balance, tracker_file_path, telegram_config, logger,
                                 df=data_with_indicators, committed_bands=committed_bands)


    except ccxt.AuthenticationError as e:
        logger.critical(f"Authentifizierungsfehler für {symbol}: {e}. API-Keys prüfen!")
        # Guardian sollte dies fangen, aber zusätzliche Logs schaden nicht
        raise # Fehler weitergeben, damit Guardian ihn sieht

    except ccxt.InsufficientFunds as e:
        logger.error(f"Fehler: Nicht genügend Guthaben für {symbol}. {e}")
        message = f"🚨 *Guthabenfehler* bei {account_name} ({symbol}):\nNicht genügend Guthaben für die Aktion."
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
        # Nicht weiter werfen, damit der Prozess nicht ständig neu startet? ODER doch? -> Doch, Guardian soll es mitbekommen.
        raise e

    except ccxt.NetworkError as e:
        logger.error(f"Netzwerkfehler bei der Kommunikation mit der Börse für {symbol}: {e}")
        # Nicht weiter werfen, erneuter Versuch im nächsten Zyklus wahrscheinlich erfolgreich

    except ccxt.ExchangeError as e:
        logger.error(f"Börsenfehler für {symbol}: {e}", exc_info=False)
        # Potenziell kritisch, aber Prozess nicht unbedingt beenden? Hängt vom Fehler ab.
        # Wenn es z.B. "Order not found" ist, ist es nicht kritisch.
        # Sende Nachricht, aber lasse den Prozess weiterlaufen.
        message = f"⚠️ *Börsenfehler* bei {account_name} ({symbol}):\n`{type(e).__name__}: {e}`"
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
        # raise e # Optional: Werfen, wenn der Prozess gestoppt werden soll

    except Exception as e:
        logger.critical(f"Unerwarteter kritischer Fehler im Handelszyklus für {symbol}: {e}", exc_info=True)
        message = f"💥 *Kritischer Fehler* im Trade Cycle für {account_name} ({symbol}):\n`{type(e).__name__}: {e}`"
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
        raise e # Fehler weiter werfen für Guardian

    finally:
        logger.info(f"===== Handelszyklus für {symbol} ({timeframe}) abgeschlossen =====")
