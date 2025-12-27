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

from ltbbot.utils.telegram import send_message
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals
from ltbbot.utils.exchange import Exchange # Import hinzugefügt, falls Type Hinting verwendet wird (optional)

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
    """Prüft, ob ein von dieser Strategie gesetzter SL ausgelöst wurde."""
    tracker_info = read_tracker_file(tracker_file_path)
    current_sl_ids = tracker_info.get("stop_loss_ids", [])
    if not current_sl_ids:
        logger.debug("Keine aktiven SL-Order-IDs im Tracker gefunden.")
        return False # Kein SL war gesetzt oder wurde verfolgt

    logger.debug(f"Prüfe {len(current_sl_ids)} SL-Order-IDs im Tracker: {current_sl_ids}")

    try:
        # Hole die letzten ~10 geschlossenen Trigger Orders (mehr Puffer)
        # Wichtig: 'fetchClosedOrders' könnte zuverlässiger sein, falls 'fetchOrders' nicht alle Trigger liefert
        closed_triggers = []
        if exchange.exchange.has['fetchClosedOrders']:
             # Einige Börsen benötigen 'stop': True auch hier
             params = {'stop': True} if 'bitget' in exchange.exchange.id else {}
             closed_triggers = exchange.exchange.fetchClosedOrders(symbol, limit=10, params=params)
             # Filtere manuell nach stopPrice, da fetchClosedOrders auch normale Orders liefern kann
             closed_triggers = [o for o in closed_triggers if o.get('stopPrice') is not None]
        elif exchange.exchange.has['fetchOrders']: # Fallback
             params = {'stop': True} if 'bitget' in exchange.exchange.id else {}
             all_orders = exchange.exchange.fetchOrders(symbol, limit=20, params=params) # Mehr holen
             closed_triggers = [o for o in all_orders if o.get('stopPrice') is not None and o['status'] in ['closed', 'canceled']]
        else:
             logger.warning("Weder fetchClosedOrders noch fetchOrders wird unterstützt, um SL-Trigger zu prüfen.")
             return False

        if not closed_triggers:
            logger.debug(f"Keine kürzlich geschlossenen Trigger-Orders für {symbol} gefunden.")
            # Sicherheitshalber prüfen, ob die Orders noch offen sind
            open_triggers = exchange.fetch_open_trigger_orders(symbol)
            open_trigger_ids = {o['id'] for o in open_triggers}
            still_open_sl_ids = [sl_id for sl_id in current_sl_ids if sl_id in open_trigger_ids]
            if set(still_open_sl_ids) != set(current_sl_ids):
                 logger.info("Einige SL-IDs aus dem Tracker sind nicht mehr offen. Aktualisiere Tracker.")
                 tracker_info["stop_loss_ids"] = still_open_sl_ids
                 update_tracker_file(tracker_file_path, tracker_info)
            return False

        triggered_sl_found = False
        pos_side = None # Seite der Position, die geschlossen wurde

        logger.debug(f"Prüfe {len(closed_triggers)} geschlossene Trigger Orders gegen Tracker-IDs.")
        for closed_order in closed_triggers:
            closed_id = closed_order['id']
            if closed_id in current_sl_ids:
                # Ein bekannter SL wurde geschlossen. Status 'closed' bedeutet meistens Auslösung.
                # 'canceled' wird ignoriert, da wir sie selbst stornieren.
                if closed_order.get('status') == 'closed':
                    logger.warning(f"🚨 STOP LOSS wurde für {symbol} ausgelöst! Order ID: {closed_id}")
                    triggered_sl_found = True
                    # Die Seite der *Position* ist das Gegenteil der SL-Order-Seite
                    pos_side = 'long' if closed_order['side'] == 'sell' else 'short'
                    break # Nur der erste gefundene Trigger zählt

        if triggered_sl_found:
            # Update Tracker: Setze Status auf 'stop_loss_triggered' und merke dir die Seite
            update_tracker_file(tracker_file_path, {
                "status": "stop_loss_triggered",
                "last_side": pos_side,
                "stop_loss_ids": [] # IDs löschen, da SL ausgelöst/geschlossen
            })
            return True
        else:
            # Wenn keiner der bekannten SLs als 'closed' gefunden wurde,
            # prüfen wir sicherheitshalber nochmal, ob sie noch offen sind
            # (redundant zur Prüfung oben, aber sicher ist sicher)
            open_triggers = exchange.fetch_open_trigger_orders(symbol)
            open_trigger_ids = {o['id'] for o in open_triggers}
            still_open_sl_ids = [sl_id for sl_id in current_sl_ids if sl_id in open_trigger_ids]
            if set(still_open_sl_ids) != set(current_sl_ids):
                logger.info("Einige SL-IDs aus dem Tracker sind nicht mehr offen (erneute Prüfung). Aktualisiere Tracker.")
                tracker_info["stop_loss_ids"] = still_open_sl_ids
                update_tracker_file(tracker_file_path, tracker_info)
            else:
                 logger.debug("Keine ausgelösten SLs gefunden. Alle bekannten SLs sind entweder noch offen oder wurden nicht als 'closed' gemeldet.")
            return False

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
            tracker_info.update({
                "status": "take_profit_triggered",
                "take_profit_ids": [],
            })
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

# --- Positions-Management ---

def manage_existing_position(exchange: Exchange, position: dict, band_prices: dict, params: dict, tracker_file_path: str, logger: logging.Logger):
    """Verwaltet eine bestehende Position: Aktualisiert TP und SL."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    pos_side = position['side']
    logger.info(f"Verwalte bestehende {pos_side}-Position für {symbol} (Größe: {position.get('contracts', 'N/A')}).")

    # Alte TP/SL Orders wurden bereits zu Beginn von full_trade_cycle storniert

    # Sicherheits-Check: Existieren TP und SL für diese Position?
    open_triggers = exchange.fetch_open_trigger_orders(symbol)
    tp_exists = False
    sl_exists = False
    for order in open_triggers:
        if order.get('reduceOnly') and order.get('side') != position['side']:
            # TP/SL sind immer reduceOnly und entgegengesetzte Seite
            if order.get('type', '').lower() == 'market' and order.get('triggerPrice'):
                # Heuristik: TP ist näher am Average, SL weiter weg
                trigger_price = float(order.get('triggerPrice', 0))
                avg_entry_price = float(position.get('entryPrice', position.get('info', {}).get('avgOpenPrice', 0)))
                if position['side'] == 'long':
                    if trigger_price > avg_entry_price:
                        tp_exists = True
                    elif trigger_price < avg_entry_price:
                        sl_exists = True
                else:
                    if trigger_price < avg_entry_price:
                        tp_exists = True
                    elif trigger_price > avg_entry_price:
                        sl_exists = True

    if not tp_exists or not sl_exists:
        logger.warning(f"Sicherheits-Check: TP vorhanden? {tp_exists}, SL vorhanden? {sl_exists}. Fehlt etwas, wird nachgetragen!")

    # Neuen TP (am Moving Average) und SL setzen, falls sie fehlen
    amount_contracts = position['contracts']
    try:
        amount_contracts_float = float(amount_contracts)
        if amount_contracts_float == 0:
             logger.warning("Positionsgröße ist 0, kann TP/SL nicht setzen.")
             return
    except (ValueError, TypeError) as e:
        logger.error(f"Konnte Positionsgröße ('{amount_contracts}') nicht in Float umwandeln: {e}")
        return

    new_sl_ids = []
    new_tp_ids = []

    try:
        # Neuer Take Profit (Trigger Market am aktuellen Durchschnitt mit Mindestabstand)
        tp_price_base = band_prices.get('average')
        if tp_price_base is None or pd.isna(tp_price_base) or tp_price_base <= 0:
            logger.error("Ungültiger Average-Preis für TP. Überspringe TP-Platzierung.")
        else:
            # Stelle Mindestabstand von 0.5% zum Entry sicher
            min_tp_distance_pct = 0.005
            if pos_side == 'long':
                tp_price = max(tp_price_base, avg_entry_price * (1 + min_tp_distance_pct))
            else:  # short
                tp_price = min(tp_price_base, avg_entry_price * (1 - min_tp_distance_pct))

            tp_side = 'sell' if pos_side == 'long' else 'buy'

            # Native trailing TP falls konfiguriert
            use_native_tp = params.get('risk', {}).get('use_native_trailing_tp', False)
            tp_callback_rate = params.get('risk', {}).get('tp_trailing_callback_rate_pct', 0.5) / 100.0
            tp_activation_delta = params.get('strategy', {}).get('tp_activation_delta_pct', 0.5) / 100.0

            if use_native_tp:
                # activation price: leicht über/unter dem Entry, oder basierend auf tp_price
                if pos_side == 'long':
                    activation_price = max(tp_price, avg_entry_price * (1 + tp_activation_delta))
                else:
                    activation_price = min(tp_price, avg_entry_price * (1 - tp_activation_delta))
                try:
                    resp = exchange.place_trailing_stop_order(
                        symbol=symbol,
                        side=tp_side,
                        amount=amount_contracts_float,
                        activation_price=activation_price,
                        callback_rate_decimal=tp_callback_rate,
                        params={'reduceOnly': True}
                    )
                    # Versuche, eine ID aus der Antwort zu extrahieren
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
                    logger.info(f"Neuen TP für {pos_side} @ {tp_price:.4f} gesetzt (Entry war @ {avg_entry_price:.4f}).")
            else:
                tp_order = exchange.place_trigger_market_order(symbol, tp_side, amount_contracts_float, tp_price, reduce=True)
                if tp_order and 'id' in tp_order:
                    new_tp_ids.append(tp_order['id'])
                logger.info(f"Neuen TP für {pos_side} @ {tp_price:.4f} gesetzt (Entry war @ {avg_entry_price:.4f}).")
            time.sleep(0.1) # Kleine Pause

        # Neuer Stop Loss (basierend auf ursprünglichem Entry und SL-Prozentsatz)
        # Versuche, den Entry-Preis zu bekommen (kann in 'entryPrice' oder 'info' stehen)
        avg_entry_price_str = position.get('entryPrice', position.get('info', {}).get('avgOpenPrice')) # Bitget verwendet oft avgOpenPrice
        if avg_entry_price_str is None:
             # Fallback, wenn nichts gefunden wurde (unwahrscheinlich, aber sicher)
             avg_entry_price_str = position.get('info', {}).get('openPriceAvg')

        if avg_entry_price_str is None:
            logger.error("Konnte Einstiegspreis für SL-Berechnung nicht ermitteln!")
            # Versuche trotzdem, SL basierend auf Mark Price zu setzen? Eher nicht.
            return
        else:
            try:
                avg_entry_price = float(avg_entry_price_str)
            except (ValueError, TypeError):
                 logger.error(f"Konnte Entry Preis '{avg_entry_price_str}' nicht in Float umwandeln.")
                 return

        sl_pct = risk_params['stop_loss_pct'] / 100.0
        trailing_callback_rate = risk_params.get('trailing_callback_rate_pct', 0.0) / 100.0  # Default 0% = kein Trailing
        
        if pos_side == 'long':
            sl_price = avg_entry_price * (1 - sl_pct)
            sl_side = 'sell'
        else: # short
            sl_price = avg_entry_price * (1 + sl_pct)
            sl_side = 'buy'

        # Stelle sicher, dass SL-Preis gültig ist
        if sl_price <= 0:
             logger.error(f"Ungültiger SL-Preis berechnet ({sl_price:.4f}). Überspringe SL-Platzierung.")
        else:
            # Verwende Trailing Stop Loss wenn trailing_callback_rate gesetzt ist
            if trailing_callback_rate > 0:
                try:
                    sl_order = exchange.place_trailing_stop_order(
                        symbol=symbol,
                        side=sl_side,
                        amount=amount_contracts_float,
                        activation_price=sl_price,
                        callback_rate_decimal=trailing_callback_rate
                    )
                    logger.info(f"✅ Trailing Stop Loss für {pos_side} gesetzt: Aktivierung @ {sl_price:.4f}, Callback {trailing_callback_rate*100:.2f}%")
                except (ccxt.NotSupported, AttributeError) as e:
                    logger.warning(f"⚠️ Trailing Stop nicht unterstützt, verwende normalen Stop Loss: {e}")
                    sl_order = exchange.place_trigger_market_order(symbol, sl_side, amount_contracts_float, sl_price, reduce=True)
                    logger.info(f"Neuen SL für {pos_side} @ {sl_price:.4f} gesetzt.")
            else:
                sl_order = exchange.place_trigger_market_order(symbol, sl_side, amount_contracts_float, sl_price, reduce=True)
                logger.info(f"Neuen SL für {pos_side} @ {sl_price:.4f} gesetzt.")
            
            if sl_order and 'id' in sl_order:
                new_sl_ids.append(sl_order['id'])

    except ccxt.InsufficientFunds as e:
         logger.error(f"Nicht genügend Guthaben zum Setzen von TP/SL (sollte bei reduceOnly nicht passieren): {e}")
    except ccxt.ExchangeError as e:
         # Spezifische Fehler wie "Trigger price is too close" behandeln
         logger.warning(f"Börsenfehler beim Setzen von TP/SL für {symbol}: {e}")
         # Hier könnte man versuchen, den Preis leicht anzupassen und es erneut zu versuchen
    except Exception as e:
        logger.error(f"Fehler beim Setzen von neuem TP/SL für {symbol}: {e}", exc_info=True)
        # Versuchen aufzuräumen (erneut canceln), falls Teilaufträge platziert wurden
        cancel_strategy_orders(exchange, symbol, logger)

    # Tracker mit neuen SL IDs aktualisieren (alte werden durch cancel überschrieben)
    tracker_info = read_tracker_file(tracker_file_path)
    tracker_info["stop_loss_ids"] = new_sl_ids # Nur die neu gesetzten IDs speichern
    if new_tp_ids:
        tracker_info["take_profit_ids"] = new_tp_ids
    update_tracker_file(tracker_file_path, tracker_info)


# --- Entry Order Platzierung ---

def place_entry_orders(exchange: Exchange, band_prices: dict, params: dict, balance: float, tracker_file_path: str, telegram_config: dict, logger: logging.Logger):
    """Platziert die gestaffelten Entry-, TP- und SL-Orders basierend auf Risiko."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    strategy_params = params['strategy']
    behavior_params = params['behavior'].copy()  # Copy um zu modifizieren
    account_name = exchange.account.get('name', 'Standard-Account')

    logger.info(f"Platziere neue Entry-Orders für {symbol} (Risikobasierte Größe). Aktueller Saldo: {balance:.2f} USDT")
    
    # Marktregime prüfen
    regime = band_prices.get('regime', 'UNCERTAIN')
    trend_direction = band_prices.get('trend_direction', 'NEUTRAL')
    adx = band_prices.get('adx')
    price_distance_pct = band_prices.get('price_distance_pct')
    logger.info(f"📊 Marktregime: {regime} | Trend: {trend_direction} | ADX: {adx} | price_distance_pct: {price_distance_pct}")

    # NEU: Bei STRONG_TREND sofort abbrechen, keine Trigger platzieren
    if regime == "STRONG_TREND":
        logger.warning(f"⚠️ STRONG_TREND erkannt - KEINE neuen Trigger/Entries werden platziert! (ADX={adx})")
        return

    # Trend-Bias anwenden (asymmetrisches Trading)
    if trend_direction == "UPTREND":
        # Im Uptrend: Keine Longs (nur Shorts wenn überhaupt)
        behavior_params['use_longs'] = False
        logger.warning(f"⚠️ UPTREND erkannt - Long-Entries DEAKTIVIERT")
    elif trend_direction == "DOWNTREND":
        # Im Downtrend: Keine Shorts (nur Longs wenn überhaupt)
        behavior_params['use_shorts'] = False
        logger.warning(f"⚠️ DOWNTREND erkannt - Short-Entries DEAKTIVIERT")

    # Parameter holen
    leverage = risk_params['leverage']
    risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Risiko pro Layer aus Config
    stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0 # SL % aus Config
    
    # ATR-basierte Stop-Loss Anpassung (falls aktiviert in Config)
    use_atr_sl = risk_params.get('use_atr_stop_loss', False)  # Neuer Config-Parameter
    if use_atr_sl:
        logger.info("🎯 ATR-basierte Stop-Loss Anpassung aktiviert")
        stop_loss_pct_param = calculate_atr_adjusted_stop_loss(exchange, symbol, stop_loss_pct_param, logger)
    
    # Dynamische Risiko-Anpassung basierend auf Performance
    reduce_risk, risk_reason = should_reduce_risk(tracker_file_path)
    if reduce_risk:
        logger.warning(f"🛡️ RISIKO-REDUKTION aktiv: {risk_reason}")
        leverage = max(1, leverage // 2)  # Halbiere Hebel, mindestens 1x
        risk_per_entry_pct = risk_per_entry_pct * 0.5  # Halbiere Positionsgröße
        logger.info(f"   Neuer Hebel: {leverage}x | Neues Risiko: {risk_per_entry_pct:.2f}%")
    
    # Stop-Loss breiter im Trend-Markt (weniger Whipsaws)
    if regime == "TREND" or regime == "STRONG_TREND":
        stop_loss_pct_param *= 1.5  # 50% breitere SLs
        logger.info(f"📈 Trend-Markt: Stop-Loss erweitert auf {stop_loss_pct_param*100:.2f}%")
    num_envelopes = len(strategy_params['envelopes'])
    min_amount_tradable = exchange.fetch_min_amount_tradable(symbol)
    trigger_delta_pct_cfg = strategy_params.get('trigger_price_delta_pct', 0.05) / 100.0

    # *** RISIKOBASIS: Startkapital oder aktueller Saldo? ***
    # Wähle EINE der folgenden Optionen aus:

    # Option 1: Risiko basiert auf ANFANGSKAPITAL (konsistent mit korrigiertem Backtester)
    # Annahme: 'initial_capital_live' ist in der config_...json definiert
    initial_capital_live = params.get('initial_capital_live', balance if balance > 1 else 1000) # Fallback auf aktuellen Saldo oder 1000
    risk_base_capital = initial_capital_live
    logger.info(f"Risikoberechnung basiert auf initialem Kapital: {risk_base_capital:.2f} USDT")

    # Option 2: Risiko basiert auf AKTUELLEM KONTOSTAND (führt zu Compounding)
    # risk_base_capital = balance
    # logger.info(f"Risikoberechnung basiert auf aktuellem Kontostand: {risk_base_capital:.2f} USDT")
    # --------------------------------------------------------

    new_sl_ids = []
    new_tp_ids = []

    # --- Long Orders ---
    if behavior_params.get('use_longs', True):
        side = 'buy'
        logger.info(f"Prüfe Long Entry Bands: {band_prices.get('long', [])}")
        for i, entry_limit_price in enumerate(band_prices.get('long', [])):
            if entry_limit_price is None or pd.isna(entry_limit_price) or entry_limit_price <= 0:
                logger.warning(f"Ungültiger Long-Entry-Preis ({entry_limit_price}) für Band {i+1}. Überspringe.")
                continue

            try:
                # 1. Risiko in USD berechnen (basierend auf gewählter Basis)
                risk_amount_usd = risk_base_capital * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0:
                    logger.warning(f"Risk amount <= 0 ({risk_amount_usd:.2f}) für Layer {i+1}. Skipping.")
                    continue

                # 2. SL-Preis und Distanz berechnen
                entry_price_for_calc = entry_limit_price
                sl_price = entry_price_for_calc * (1 - stop_loss_pct_param)
                if sl_price <=0 :
                     logger.warning(f"Negativer oder Null SL-Preis ({sl_price:.4f}) berechnet für Entry {entry_price_for_calc:.4f}. Überspringe Layer {i+1}.")
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

                # 5. Benötigte Margin (nur zur Info)
                margin_required = (amount_coins * entry_price_for_calc) / leverage
                logger.debug(f"Long Layer {i+1}: Risk={risk_amount_usd:.2f}$, Size={amount_coins:.8f}, MarginReq={margin_required:.2f}$ (Verfügbar ca.: {balance:.2f})")

                # KORRIGIERT: Trigger UNTER dem Limit-Preis für Long
                # (Entry erst wenn Preis tief genug gefallen ist)
                entry_trigger_price = entry_limit_price * (1 - trigger_delta_pct_cfg)


                # ZUERST TP platzieren
                tp_price = band_prices.get('average')
                if tp_price is None or pd.isna(tp_price) or tp_price <= 0:
                    logger.error("Ungültiger Average-Preis für TP. Überspringe TP.")
                else:
                    # Native trailing TP falls aktiviert
                    use_native_tp = risk_params.get('use_native_trailing_tp', False)
                    tp_callback_rate = risk_params.get('tp_trailing_callback_rate_pct', 0.5) / 100.0
                    tp_activation_delta = strategy_params.get('tp_activation_delta_pct', 0.5) / 100.0
                    if use_native_tp:
                        activation_price = max(tp_price, entry_limit_price * (1 + tp_activation_delta))
                        try:
                            resp = exchange.place_trailing_stop_order(
                                symbol=symbol, side='sell', amount=amount_coins,
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
                            logger.debug(f"  Native TP(TSL) für Long Entry {i+1} platziert. activation={activation_price:.4f}, id={tp_id}")
                        except Exception as e:
                            logger.warning(f"Native Trailing-TP fehlgeschlagen, fallback: {e}")
                            tp_order = exchange.place_trigger_market_order(
                                symbol=symbol, side='sell', amount=amount_coins,
                                trigger_price=tp_price, reduce=True
                            )
                            if tp_order and 'id' in tp_order:
                                new_tp_ids.append(tp_order['id'])
                            logger.debug(f"  TP für Long Entry {i+1} @ {tp_price:.4f} platziert. ID={tp_order.get('id') if tp_order else 'N/A'}")
                    else:
                        tp_order = exchange.place_trigger_market_order(
                            symbol=symbol, side='sell', amount=amount_coins,
                            trigger_price=tp_price, reduce=True
                        )
                        if tp_order and 'id' in tp_order:
                            new_tp_ids.append(tp_order['id'])
                        logger.debug(f"  TP für Long Entry {i+1} @ {tp_price:.4f} platziert. ID={tp_order.get('id') if tp_order else 'N/A'}")
                    time.sleep(0.1)

                # Dann SL platzieren
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='sell', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Long Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids.append(sl_order['id'])
                time.sleep(0.1)

                # Dann Entry Order (Trigger Limit)
                entry_order = exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                logger.info(f"✅ Long Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")
                time.sleep(0.1)

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Long-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders für DIESE SEITE.")
                break # Bei InsufficientFunds weitere Layer für diese Seite abbrechen
            except ccxt.ExchangeError as e:
                 logger.error(f"Börsenfehler beim Platzieren der Long-Order-Gruppe {i+1}: {e}")
                 # Hier könnte man spezifische Fehler behandeln, z.B. Preis zu weit weg etc.
            except Exception as e:
                logger.error(f"Allg. Fehler beim Platzieren der Long-Order-Gruppe {i+1}: {e}", exc_info=True)
                # Nicht abbrechen, versuche nächsten Layer

    # --- Short Orders ---
    if behavior_params.get('use_shorts', True):
        side = 'sell'
        logger.info(f"Prüfe Short Entry Bands: {band_prices.get('short', [])}")
        for i, entry_limit_price in enumerate(band_prices.get('short', [])):
            if entry_limit_price is None or pd.isna(entry_limit_price) or entry_limit_price <= 0:
                logger.warning(f"Ungültiger Short-Entry-Preis ({entry_limit_price}) für Band {i+1}. Überspringe.")
                continue

            try:
                # 1. Risiko in USD berechnen (basierend auf gewählter Basis)
                risk_amount_usd = risk_base_capital * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0: continue

                # 2. SL-Preis und Distanz berechnen
                entry_price_for_calc = entry_limit_price
                sl_price = entry_price_for_calc * (1 + stop_loss_pct_param)
                if sl_price <=0 : continue # Ungültiger Preis
                sl_distance_price = abs(entry_price_for_calc - sl_price)
                if sl_distance_price <= 0: continue

                # 3. Positionsgröße (amount_coins) berechnen
                amount_coins = risk_amount_usd / sl_distance_price

                # 4. Mindestmenge prüfen
                if amount_coins < min_amount_tradable:
                    logger.warning(f"Berechnete Short-Menge {amount_coins:.8f} für Layer {i+1} liegt unter Minimum {min_amount_tradable:.8f}. Überspringe.")
                    continue

                # 5. Benötigte Margin (nur zur Info)
                margin_required = (amount_coins * entry_price_for_calc) / leverage
                logger.debug(f"Short Layer {i+1}: Risk={risk_amount_usd:.2f}$, Size={amount_coins:.8f}, MarginReq={margin_required:.2f}$ (Verfügbar ca.: {balance:.2f})")

                # KORRIGIERT: Trigger ÜBER dem Limit-Preis für Short
                # (Entry erst wenn Preis hoch genug gestiegen ist)
                entry_trigger_price = entry_limit_price * (1 + trigger_delta_pct_cfg)


                # ZUERST TP platzieren
                tp_price = band_prices.get('average')
                if tp_price is None or pd.isna(tp_price) or tp_price <= 0:
                    logger.error("Ungültiger Average-Preis für TP. Überspringe TP.")
                else:
                    # Native trailing TP falls aktiviert
                    use_native_tp = risk_params.get('use_native_trailing_tp', False)
                    tp_callback_rate = risk_params.get('tp_trailing_callback_rate_pct', 0.5) / 100.0
                    tp_activation_delta = strategy_params.get('tp_activation_delta_pct', 0.5) / 100.0
                    if use_native_tp:
                        activation_price = min(tp_price, entry_limit_price * (1 - tp_activation_delta))
                        try:
                            resp = exchange.place_trailing_stop_order(
                                symbol=symbol, side='buy', amount=amount_coins,
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
                            logger.debug(f"  Native TP(TSL) für Short Entry {i+1} platziert. activation={activation_price:.4f}, id={tp_id}")
                        except Exception as e:
                            logger.warning(f"Native Trailing-TP fehlgeschlagen, fallback: {e}")
                            tp_order = exchange.place_trigger_market_order(
                                symbol=symbol, side='buy', amount=amount_coins,
                                trigger_price=tp_price, reduce=True
                            )
                            if tp_order and 'id' in tp_order:
                                new_tp_ids.append(tp_order['id'])
                            logger.debug(f"  TP für Short Entry {i+1} @ {tp_price:.4f} platziert. ID={tp_order.get('id') if tp_order else 'N/A'}")
                    else:
                        tp_order = exchange.place_trigger_market_order(
                            symbol=symbol, side='buy', amount=amount_coins,
                            trigger_price=tp_price, reduce=True
                        )
                        if tp_order and 'id' in tp_order:
                            new_tp_ids.append(tp_order['id'])
                        logger.debug(f"  TP für Short Entry {i+1} @ {tp_price:.4f} platziert. ID={tp_order.get('id') if tp_order else 'N/A'}")
                    time.sleep(0.1)

                # Dann SL platzieren
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='buy', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Short Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids.append(sl_order['id'])
                time.sleep(0.1)

                # Dann Entry Order (Trigger Limit)
                entry_order = exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                logger.info(f"✅ Short Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")
                time.sleep(0.1)

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Short-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders für DIESE SEITE.")
                break
            except ccxt.ExchangeError as e:
                 logger.error(f"Börsenfehler beim Platzieren der Short-Order-Gruppe {i+1}: {e}")
            except Exception as e:
                logger.error(f"Allg. Fehler beim Platzieren der Short-Order-Gruppe {i+1}: {e}", exc_info=True)


    # Tracker mit neuen SL IDs aktualisieren (nur wenn Orders platziert wurden)
    if new_sl_ids:
        tracker_info = read_tracker_file(tracker_file_path)
        # Füge neue IDs hinzu, ohne alte zu löschen (falls manage_existing_position welche gesetzt hat - obwohl alte ja storniert wurden)
        # Sicherer ist, nur die neuen zu speichern.
        tracker_info["stop_loss_ids"] = new_sl_ids
        if new_tp_ids:
            tracker_info["take_profit_ids"] = new_tp_ids
        # WICHTIG: Wenn neue Entries platziert werden, ist der Cooldown definitiv vorbei
        tracker_info["status"] = "ok_to_trade"
        tracker_info["last_side"] = None
        update_tracker_file(tracker_file_path, tracker_info)
        logger.info(f"Tracker mit {len(new_sl_ids)} neuen SL Order IDs aktualisiert (Status: ok_to_trade).")
    elif not any(p is not None and not pd.isna(p) for p in band_prices.get('long', [])) and \
         not any(p is not None and not pd.isna(p) for p in band_prices.get('short', [])): # Keine gültigen Preise gefunden
           logger.info("Keine gültigen Entry-Preise gefunden, keine Orders platziert.")
           # Sicherstellen, dass keine alten SL-IDs im Tracker verbleiben
           tracker_info = read_tracker_file(tracker_file_path)
           if tracker_info.get("stop_loss_ids"):
               tracker_info["stop_loss_ids"] = []
               update_tracker_file(tracker_file_path, tracker_info)
    else:
           logger.info("Keine Entry-Orders platziert (ggf. Menge zu klein, Margin, Max Pos Size oder Fehler).")
            # Sicherstellen, dass keine alten SL-IDs im Tracker verbleiben
           tracker_info = read_tracker_file(tracker_file_path)
           if tracker_info.get("stop_loss_ids"):
               tracker_info["stop_loss_ids"] = []
               update_tracker_file(tracker_file_path, tracker_info)

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
        adx = band_prices.get('adx')
        price_distance_pct = band_prices.get('price_distance_pct')

        logger.info(f"📊 Marktregime: {regime} | Trend: {trend_direction} | Trading: {'✅' if trade_allowed else '❌'} | ADX: {adx} | price_distance_pct: {price_distance_pct}")

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
        tp_triggered_this_cycle = check_take_profit_trigger(exchange, symbol, tracker_file_path, logger)
        sl_triggered_this_cycle = check_stop_loss_trigger(exchange, symbol, tracker_file_path, logger)
        # Wenn SL ausgelöst wurde, wird der Tracker-Status aktualisiert

        # --- 3. Alle alten Orders der Strategie stornieren (wichtig!) ---
        # Storniert Limit- und Trigger-Orders, die von *diesem* Bot für *dieses* Symbol platziert wurden
        cancel_strategy_orders(exchange, symbol, logger)

        # --- 4. Tracker-Status prüfen ("Cooldown" nach SL) ---
        tracker_info = read_tracker_file(tracker_file_path)
        current_status = tracker_info['status']
        last_side_sl = tracker_info.get('last_side') # Seite der Position, die ausgestoppt wurde
        logger.info(f"Tracker-Status: {current_status}, Letzte SL-Seite: {last_side_sl}")

        cooldown_active = False
        if current_status == "stop_loss_triggered":
            cooldown_active = True
            # Prüfen, ob Cooldown beendet werden kann
            # Preis muss Average KREUZEN, nicht nur berühren
            average_crossed = False
            if last_side_sl == 'long' and last_price > current_average: # Preis ist ÜBER dem Average nach Long-SL
                average_crossed = True
            elif last_side_sl == 'short' and last_price < current_average: # Preis ist UNTER dem Average nach Short-SL
                average_crossed = True
            # Es könnte sein, dass last_side_sl None ist, obwohl Status triggered ist (Fehlerfall)
            elif last_side_sl is None:
                 logger.warning("Cooldown aktiv, aber 'last_side' ist None. Setze Status sicherheitshalber zurück.")
                 average_crossed = True # Reset erlauben

            if average_crossed:
                logger.info(f"Preis hat Average gekreuzt nach SL ({last_side_sl}). Setze Status zurück auf 'ok_to_trade'.")
                tracker_info['status'] = 'ok_to_trade'
                tracker_info['last_side'] = None # Seite zurücksetzen
                tracker_info['stop_loss_ids'] = [] # Sicherstellen, dass IDs leer sind
                update_tracker_file(tracker_file_path, tracker_info)
                cooldown_active = False # Cooldown für diesen Zyklus aufgehoben
            else:
                logger.info(f"Bot ist im Cooldown-Modus ('stop_loss_triggered' für {last_side_sl}). Keine neuen Entries bis Preis Average kreuzt.")

        # --- 5. Offene Position prüfen und verwalten ---
        position_list = exchange.fetch_open_positions(symbol)
        position = position_list[0] if position_list else None # Nimm die erste (sollte nur eine geben)

        if position:
            # Position ist offen -> TP/SL aktualisieren (auch im Cooldown!)
            manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger)
            logger.info(f"Position für {symbol} ist offen ({position['side']} {position['contracts']}). Nur TP/SL verwaltet.")
            # Telegram-Benachrichtigung NUR wenn Position NEU eröffnet wurde
            tracker_info = read_tracker_file(tracker_file_path)
            if not tracker_info.get('notified_open', False):
                message = f"🚀 *TRADE AUSGELÖST*\nSymbol: {symbol}\nRichtung: {position['side']}\nMenge: {position['contracts']}\nEntry: {position['entryPrice']}"
                send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
                tracker_info['notified_open'] = True
                update_tracker_file(tracker_file_path, tracker_info)
            # Keine neuen Entry-Orders platzieren, wenn schon eine Position offen ist

        elif cooldown_active:
              logger.info("Keine Position offen, aber Cooldown aktiv. Keine neuen Entries.")
              # Sicherstellen, dass keine SL IDs im Tracker sind
              if tracker_info.get("stop_loss_ids"):
                   tracker_info["stop_loss_ids"] = []
                   update_tracker_file(tracker_file_path, tracker_info)

        elif sl_triggered_this_cycle:
              # Direkt nach SL-Trigger in *diesem* Zyklus keine neuen Entries,
              # auch wenn Cooldown formal aufgehoben wäre (verhindert sofortigen Wiedereinstieg)
              logger.warning("SL wurde in diesem Zyklus ausgelöst. Überspringe Platzierung neuer Entry-Orders für diesen Lauf.")
              # Tracker Status wurde schon in check_stop_loss_trigger gesetzt

        else: # Keine Position offen, kein Cooldown, kein SL in diesem Zyklus
              logger.info(f"Keine offene Position für {symbol} und Cooldown nicht aktiv.")
              current_balance = exchange.fetch_balance_usdt()
              if current_balance <= 1: # Mindestguthaben
                  logger.error(f"Guthaben ({current_balance:.2f} USDT) zu gering zum Platzieren von Entry-Orders.")
                  # Sende evtl. Telegram Nachricht
                  message = f"📉 *Guthaben zu gering* bei {account_name} ({symbol}): {current_balance:.2f} USDT."
                  send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
                  return # Zyklus hier beenden

              # Setze Margin Mode und Leverage VOR dem Order platzieren
              try:
                  risk_params = params['risk']
                  exchange.set_margin_mode(symbol, margin_mode=risk_params['margin_mode'])
                  # Kurze Pause nach Margin Mode setzen kann bei Bitget helfen
                  time.sleep(0.5)
                  exchange.set_leverage(symbol, margin_mode=risk_params['margin_mode'], leverage=risk_params['leverage'])
                  time.sleep(0.5) # Weitere kurze Pause
              except Exception as e:
                  logger.warning(f"Konnte Margin Mode/Leverage nicht setzen (evtl. schon korrekt?): {e}")

              # Neue Entry Orders platzieren
              place_entry_orders(exchange, band_prices, params, current_balance, tracker_file_path, telegram_config, logger)


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
