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
    """
    Storniert alle offenen Limit- und Trigger-Orders für die Strategie.
    WICHTIG: TP/SL Orders werden NIE storniert, wenn eine Position offen ist!
    
    Args:
        exchange: Exchange-Instanz
        symbol: Trading-Symbol
        logger: Logger-Instanz
        tracker_file_path: Optional - Pfad zur Tracker-Datei für bekannte TP/SL IDs
    """
    cancelled_count = 0
    try:
        # KRITISCH: Prüfe ob eine Position offen ist
        position_list = exchange.fetch_open_positions(symbol)
        has_open_position = len(position_list) > 0
        
        if has_open_position:
            logger.info(f"🛡️ Position offen für {symbol} - TP/SL Orders werden GESCHÜTZT!")
        
        # Lade bekannte TP/SL IDs aus Tracker (falls vorhanden)
        protected_order_ids = set()
        if tracker_file_path and os.path.exists(tracker_file_path):
            try:
                tracker_info = read_tracker_file(tracker_file_path)
                protected_order_ids.update(tracker_info.get("stop_loss_ids", []))
                protected_order_ids.update(tracker_info.get("take_profit_ids", []))
                if protected_order_ids:
                    logger.debug(f"Geschützte Order-IDs aus Tracker: {protected_order_ids}")
            except Exception as e:
                logger.warning(f"Konnte Tracker nicht lesen für Order-Schutz: {e}")
        
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
<<<<<<< HEAD
            # WICHTIG: Trigger-Orders, die als reduceOnly markiert sind (TP/SL),
            # nicht automatisch stornieren — das führt sonst dazu, dass TPs
            # bei jedem Master-Zyklus verschwinden und wieder neu gesetzt werden.
            if order.get('reduceOnly'):
                logger.debug(f"Überspringe reduceOnly Trigger Order {order['id']} ({order.get('side')} {order.get('amount')} @ Trigger {order.get('stopPrice', 'N/A')}).")
                continue
=======
            order_id = order.get('id')
            
            # KRITISCH: Wenn Position offen ist, NIE TP/SL stornieren!
            if has_open_position:
                # Prüfe ob es eine TP/SL Order ist (mehrere Kriterien für Robustheit)
                is_reduce_only = order.get('reduceOnly', False)
                is_in_tracker = order_id in protected_order_ids
                is_market_trigger = (
                    order.get('type', '').lower() == 'market' and 
                    order.get('triggerPrice') is not None
                )
                
                # Heuristik: Wenn es eine Market-Trigger-Order ist UND reduceOnly, ist es definitiv TP/SL
                # Oder wenn es im Tracker als TP/SL bekannt ist
                is_tp_sl = is_reduce_only or is_in_tracker or is_market_trigger
                
                if is_tp_sl:
                    logger.debug(f"🛡️ SCHUTZ: Überspringe TP/SL Order {order_id} (reduceOnly={is_reduce_only}, inTracker={is_in_tracker}, marketTrigger={is_market_trigger})")
                    continue
            
            # Wenn keine Position offen ist, prüfe reduceOnly (wie bisher)
            elif order.get('reduceOnly'):
                logger.debug(f"Überspringe reduceOnly Trigger Order {order['id']} ({order.get('side')} {order.get('amount')} @ Trigger {order.get('stopPrice', 'N/A')}).")
                continue
            
            # Storniere nur Entry-Orders (Trigger Limit ohne reduceOnly)
            try:
                exchange.cancel_trigger_order(order_id, symbol)
                cancelled_count += 1
                logger.info(f"Trigger Order {order_id} ({order['side']} {order['amount']} @ Trigger {order.get('stopPrice', 'N/A')}) storniert.")
                time.sleep(0.1) # Kleine Pause
            except ccxt.OrderNotFound:
                logger.debug(f"Trigger Order {order_id} war bereits geschlossen/storniert.")
            except Exception as e:
                logger.warning(f"Konnte Trigger Order {order_id} nicht stornieren: {e}")

        if cancelled_count > 0:
            logger.info(f"{cancelled_count} offene Order(s) für {symbol} erfolgreich storniert.")
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
        cancel_strategy_orders(exchange, symbol, logger, tracker_file_path)

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
