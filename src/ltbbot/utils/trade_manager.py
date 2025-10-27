# src/ltbbot/utils/trade_manager.py
import logging
import time
import ccxt
import os
import json
from datetime import datetime
import sys
import pandas as pd # Hinzugefügt für pd.isna Check

# Pfade für die Tracker-Datei definieren
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TRACKER_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker')

# Sicherstellen, dass das src-Verzeichnis im PYTHONPATH ist (kann in manchen Setups helfen)
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.telegram import send_message
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals
from ltbbot.utils.exchange import Exchange # Import hinzugefügt, falls Type Hinting verwendet wird (optional)

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

def cancel_strategy_orders(exchange: Exchange, symbol: str, logger: logging.Logger):
    """Storniert alle offenen Limit- und Trigger-Orders für die Strategie."""
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

# --- Positions-Management ---

def manage_existing_position(exchange: Exchange, position: dict, band_prices: dict, params: dict, tracker_file_path: str, logger: logging.Logger):
    """Verwaltet eine bestehende Position: Aktualisiert TP und SL."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    pos_side = position['side']
    logger.info(f"Verwalte bestehende {pos_side}-Position für {symbol} (Größe: {position.get('contracts', 'N/A')}).")

    # Alte TP/SL Orders wurden bereits zu Beginn von full_trade_cycle storniert

    # Neuen TP (am Moving Average) und SL setzen
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

    try:
        # Neuer Take Profit (Trigger Market am aktuellen Durchschnitt)
        tp_price = band_prices.get('average')
        if tp_price is None or pd.isna(tp_price) or tp_price <= 0:
             logger.error("Ungültiger Average-Preis für TP. Überspringe TP-Platzierung.")
        else:
            tp_side = 'sell' if pos_side == 'long' else 'buy'
            exchange.place_trigger_market_order(symbol, tp_side, amount_contracts_float, tp_price, reduce=True)
            logger.info(f"Neuen TP für {pos_side} @ {tp_price:.4f} gesetzt.")
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
    update_tracker_file(tracker_file_path, tracker_info)


# --- Entry Order Platzierung ---

def place_entry_orders(exchange: Exchange, band_prices: dict, params: dict, balance: float, tracker_file_path: str, logger: logging.Logger):
    """Platziert die gestaffelten Entry-, TP- und SL-Orders basierend auf Risiko."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    strategy_params = params['strategy']
    behavior_params = params['behavior']
    account_name = exchange.account.get('name', 'Standard-Account')

    logger.info(f"Platziere neue Entry-Orders für {symbol} (Risikobasierte Größe). Aktueller Saldo: {balance:.2f} USDT")

    # Parameter holen
    leverage = risk_params['leverage']
    risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Risiko pro Layer aus Config
    stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0 # SL % aus Config
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

                # Trigger leicht über dem Limit-Preis
                entry_trigger_price = entry_limit_price * (1 + trigger_delta_pct_cfg)

                # Entry Order (Trigger Limit)
                entry_order = exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                logger.info(f"✅ Long Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")
                time.sleep(0.1)

                # Zugehöriger Take Profit (Trigger Market am Average)
                tp_price = band_prices.get('average')
                if tp_price is None or pd.isna(tp_price) or tp_price <= 0:
                     logger.error("Ungültiger Average-Preis für TP. Überspringe TP.")
                else:
                    exchange.place_trigger_market_order(
                        symbol=symbol, side='sell', amount=amount_coins,
                        trigger_price=tp_price, reduce=True
                    )
                    logger.debug(f"  TP für Long Entry {i+1} @ {tp_price:.4f} platziert.")
                    time.sleep(0.1)

                # Zugehöriger Stop Loss (Trigger Market)
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='sell', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Long Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids.append(sl_order['id'])
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

                # Trigger leicht unter dem Limit-Preis
                entry_trigger_price = entry_limit_price * (1 - trigger_delta_pct_cfg)

                # Entry Order (Trigger Limit)
                entry_order = exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                logger.info(f"✅ Short Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")
                time.sleep(0.1)

                # Zugehöriger Take Profit (Trigger Market am Average)
                tp_price = band_prices.get('average')
                if tp_price is None or pd.isna(tp_price) or tp_price <= 0:
                     logger.error("Ungültiger Average-Preis für TP. Überspringe TP.")
                else:
                    exchange.place_trigger_market_order(
                        symbol=symbol, side='buy', amount=amount_coins,
                        trigger_price=tp_price, reduce=True
                    )
                    logger.debug(f"  TP für Short Entry {i+1} @ {tp_price:.4f} platziert.")
                    time.sleep(0.1)

                # Zugehöriger Stop Loss (Trigger Market)
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='buy', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Short Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids.append(sl_order['id'])
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


        # --- 2. Prüfen, ob SL ausgelöst wurde SEIT dem letzten Lauf ---
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
              place_entry_orders(exchange, band_prices, params, current_balance, tracker_file_path, logger)


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
