# src/ltbbot/utils/trade_manager.py
import logging
import time
import ccxt
import os
import json
from datetime import datetime
import sys

# Pfade für die Tracker-Datei definieren (ersetzt trade_lock.json)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TRACKER_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker') # Eigener Ordner für Tracker

# Sicherstellen, dass das übergeordnete Verzeichnis von 'ltbbot' im sys.path ist
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.telegram import send_message
# Import der neuen Envelope-Logik (Pfad anpassen, falls nötig)
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals


def get_tracker_file_path(symbol, timeframe):
    """Generiert den Pfad zur Tracker-Datei für eine Strategie."""
    safe_filename = f"{symbol.replace('/', '-').replace(':', '-')}_{timeframe}.json"
    return os.path.join(TRACKER_DIR, safe_filename)

def read_tracker_file(file_path):
    """Liest den Status aus der Tracker-Datei."""
    if not os.path.exists(file_path):
        # Erstelle Standard-Tracker, wenn nicht vorhanden
        default_data = {"status": "ok_to_trade", "last_side": None, "stop_loss_ids": []}
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w') as f:
            json.dump(default_data, f, indent=4)
        return default_data
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        logging.error(f"Fehler beim Lesen der Tracker-Datei {file_path}. Setze auf Standard zurück.")
        default_data = {"status": "ok_to_trade", "last_side": None, "stop_loss_ids": []}
        # Versuche, die korrupte Datei zu überschreiben
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w') as f:
                json.dump(default_data, f, indent=4)
        except Exception as write_err:
             logging.error(f"Konnte korrupte Tracker-Datei nicht überschreiben: {write_err}")
        return default_data


def update_tracker_file(file_path, data):
    """Schreibt den Status in die Tracker-Datei."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Fehler beim Schreiben der Tracker-Datei {file_path}: {e}")

def cancel_strategy_orders(exchange, symbol, logger):
    """Storniert alle offenen Limit- und Trigger-Orders für die Strategie."""
    cancelled_count = 0
    try:
        # Normale Limit-Orders (könnten Reste sein)
        orders = exchange.fetch_open_orders(symbol)
        for order in orders:
            try:
                exchange.cancel_order(order['id'], symbol)
                cancelled_count += 1
                logger.debug(f"Normale Order {order['id']} storniert.")
            except ccxt.OrderNotFound:
                pass
            except Exception as e:
                logger.warning(f"Konnte normale Order {order['id']} nicht stornieren: {e}")

        # Trigger Orders (Entry, TP, SL)
        trigger_orders = exchange.fetch_open_trigger_orders(symbol)
        for order in trigger_orders:
             try:
                # Wichtig: cancel_trigger_order verwenden!
                exchange.cancel_trigger_order(order['id'], symbol)
                cancelled_count += 1
                logger.debug(f"Trigger Order {order['id']} storniert.")
             except ccxt.OrderNotFound:
                 pass
             except Exception as e:
                logger.warning(f"Konnte Trigger Order {order['id']} nicht stornieren: {e}")

        if cancelled_count > 0:
            logger.info(f"{cancelled_count} offene Order(s) für {symbol} storniert.")
        else:
            logger.info(f"Keine offenen Orders für {symbol} zum Stornieren gefunden.")
        return cancelled_count
    except Exception as e:
        logger.error(f"Fehler beim Stornieren der Orders für {symbol}: {e}", exc_info=True)
        return cancelled_count


def check_stop_loss_trigger(exchange, symbol, tracker_file_path, logger):
    """Prüft, ob ein von dieser Strategie gesetzter SL ausgelöst wurde."""
    tracker_info = read_tracker_file(tracker_file_path)
    if not tracker_info.get("stop_loss_ids"):
        return False # Kein SL war gesetzt

    try:
        closed_triggers = exchange.fetch_closed_trigger_orders(symbol) # Holt die letzten geschlossenen Trigger Orders
        if not closed_triggers:
            return False

        # Finde die neueste geschlossene Trigger Order
        latest_closed_trigger = max(closed_triggers, key=lambda x: x['timestamp'])

        if latest_closed_trigger['id'] in tracker_info['stop_loss_ids']:
            logger.warning(f"STOP LOSS wurde für {symbol} ausgelöst! Order ID: {latest_closed_trigger['id']}")
            # Update Tracker: Setze Status auf 'stop_loss_triggered' und merke dir die Seite
            pos_side = 'long' if latest_closed_trigger['side'] == 'sell' else 'short' # SL schließt die Position
            update_tracker_file(tracker_file_path, {
                "status": "stop_loss_triggered",
                "last_side": pos_side,
                "stop_loss_ids": [] # IDs löschen, da SL ausgelöst
            })
            return True
        return False
    except Exception as e:
        logger.error(f"Fehler beim Prüfen geschlossener SL-Orders für {symbol}: {e}")
        return False


def manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger):
    """Verwaltet eine bestehende Position: Aktualisiert TP und SL."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    strategy_params = params['strategy']
    logger.info(f"Verwalte bestehende {position['side']}-Position für {symbol}.")

    # 1. Alte TP/SL Orders stornieren (nur die, die zum Schließen da sind)
    cancelled_closing_orders = 0
    try:
        trigger_orders = exchange.fetch_open_trigger_orders(symbol)
        current_sl_ids = read_tracker_file(tracker_file_path).get("stop_loss_ids", [])

        for order in trigger_orders:
            # Storniere nur Orders, die zum Schließen sind (reduceOnly)
            # und entweder alte SLs sind oder der TP sind.
             is_reduce_only = order.get('reduceOnly', False) or order.get('info', {}).get('reduceOnly', False)
             is_current_sl = order['id'] in current_sl_ids

             if is_reduce_only and not is_current_sl: # Dies sollte der TP sein (oder alte SLs)
                 try:
                    exchange.cancel_trigger_order(order['id'], symbol)
                    cancelled_closing_orders+=1
                    logger.debug(f"Alte TP/SL-Order {order['id']} storniert.")
                 except ccxt.OrderNotFound:
                     pass
                 except Exception as e:
                    logger.warning(f"Konnte alte TP/SL-Order {order['id']} nicht stornieren: {e}")
    except Exception as e:
        logger.error(f"Fehler beim Stornieren alter TP/SL für {symbol}: {e}")

    if cancelled_closing_orders > 0:
        logger.info(f"{cancelled_closing_orders} alte TP/SL-Orders storniert.")

    # 2. Neuen TP (am Moving Average) und SL setzen
    amount_contracts = position['contracts'] # Menge in Kontrakten
    new_sl_ids = []

    try:
        # Neuer Take Profit (immer am aktuellen Durchschnitt)
        tp_price = band_prices['average']
        tp_side = 'sell' if position['side'] == 'long' else 'buy'
        exchange.place_trigger_market_order(symbol, tp_side, amount_contracts, tp_price, reduce=True)
        logger.info(f"Neuen TP für {position['side']} @ {tp_price:.4f} gesetzt.")

        # Neuer Stop Loss (basierend auf ursprünglichem Entry und SL-Prozentsatz)
        # Wir nehmen den Durchschnitts-Einstiegspreis der Position
        avg_entry_price = float(position.get('entryPrice') or position.get('info', {}).get('openPriceAvg', 0))
        if avg_entry_price == 0:
            logger.error("Konnte Einstiegspreis für SL-Berechnung nicht ermitteln!")
            return # Ohne Entry können wir keinen sinnvollen SL setzen

        sl_pct = risk_params['stop_loss_pct'] / 100.0 # Umwandlung von % zu Dezimal
        if position['side'] == 'long':
            sl_price = avg_entry_price * (1 - sl_pct)
            sl_side = 'sell'
        else: # short
            sl_price = avg_entry_price * (1 + sl_pct)
            sl_side = 'buy'

        sl_order = exchange.place_trigger_market_order(symbol, sl_side, amount_contracts, sl_price, reduce=True)
        logger.info(f"Neuen SL für {position['side']} @ {sl_price:.4f} gesetzt.")
        if sl_order and 'id' in sl_order:
            new_sl_ids.append(sl_order['id'])

    except Exception as e:
        logger.error(f"Fehler beim Setzen von neuem TP/SL für {symbol}: {e}", exc_info=True)
        # Versuchen aufzuräumen, falls nur eine Order platziert wurde
        cancel_strategy_orders(exchange, symbol, logger)

    # Tracker mit neuen SL IDs aktualisieren (auch wenn leer bei Fehler)
    tracker_info = read_tracker_file(tracker_file_path) # Erneut lesen, falls Status geändert wurde
    tracker_info["stop_loss_ids"] = new_sl_ids
    update_tracker_file(tracker_file_path, tracker_info)


def place_entry_orders(exchange, band_prices, params, balance, tracker_file_path, logger):
    """Platziert die gestaffelten Entry-, TP- und SL-Orders."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    strategy_params = params['strategy']
    behavior_params = params['behavior']
    account_name = exchange.account.get('name', 'Standard-Account')

    logger.info(f"Platziere neue Entry-Orders für {symbol}.")

    # Balance für Strategie berechnen
    leverage = risk_params['leverage']
    balance_fraction = risk_params['balance_fraction_pct'] / 100.0
    trading_balance = balance * balance_fraction * leverage # Gesamt-Kapital für Orders
    num_envelopes = len(strategy_params['envelopes'])
    balance_per_order = trading_balance / num_envelopes

    new_sl_ids = []
    min_amount_tradable = exchange.fetch_min_amount_tradable(symbol)

    # --- Long Orders ---
    if behavior_params.get('use_longs', True):
        for i, entry_limit_price in enumerate(band_prices['long']):
            if entry_limit_price <= 0:
                logger.warning(f"Ungültiger Long-Entry-Preis ({entry_limit_price:.4f}) für Band {i+1}. Überspringe.")
                continue

            amount = balance_per_order / entry_limit_price
            if amount < min_amount_tradable:
                 logger.warning(f"Berechnete Long-Menge {amount:.6f} für Band {i+1} liegt unter Minimum {min_amount_tradable:.6f}. Überspringe.")
                 continue

            try:
                 # Trigger leicht über dem Limit-Preis, um Ausführung zu verbessern
                 trigger_delta_pct = strategy_params.get('trigger_price_delta_pct', 0.05) / 100.0
                 entry_trigger_price = entry_limit_price * (1 + trigger_delta_pct)

                 # Entry Order (Trigger Limit)
                 exchange.place_trigger_limit_order(
                     symbol=symbol, side='buy', amount=amount,
                     trigger_price=entry_trigger_price, price=entry_limit_price
                 )
                 logger.info(f"Long Entry {i+1}/{num_envelopes} platziert: Amount={amount:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")

                 # Zugehöriger Take Profit (Trigger Market am Average)
                 tp_price = band_prices['average']
                 exchange.place_trigger_market_order(
                     symbol=symbol, side='sell', amount=amount,
                     trigger_price=tp_price, reduce=True
                 )
                 logger.debug(f"  TP für Long Entry {i+1} @ {tp_price:.4f} platziert.")

                 # Zugehöriger Stop Loss (Trigger Market)
                 sl_pct = risk_params['stop_loss_pct'] / 100.0
                 sl_price = entry_limit_price * (1 - sl_pct) # SL basiert auf dem geplanten Entry
                 sl_order = exchange.place_trigger_market_order(
                     symbol=symbol, side='sell', amount=amount,
                     trigger_price=sl_price, reduce=True
                 )
                 logger.debug(f"  SL für Long Entry {i+1} @ {sl_price:.4f} platziert.")
                 if sl_order and 'id' in sl_order:
                     new_sl_ids.append(sl_order['id'])

            except Exception as e:
                logger.error(f"Fehler beim Platzieren der Long-Order-Gruppe {i+1}: {e}", exc_info=True)
                # Hier könnten wir versuchen, teilweise platzierte Orders zu stornieren,
                # aber es ist sicherer, dies dem nächsten Durchlauf zu überlassen.

    # --- Short Orders ---
    if behavior_params.get('use_shorts', True):
        for i, entry_limit_price in enumerate(band_prices['short']):
            if entry_limit_price <= 0:
                logger.warning(f"Ungültiger Short-Entry-Preis ({entry_limit_price:.4f}) für Band {i+1}. Überspringe.")
                continue

            amount = balance_per_order / entry_limit_price
            if amount < min_amount_tradable:
                 logger.warning(f"Berechnete Short-Menge {amount:.6f} für Band {i+1} liegt unter Minimum {min_amount_tradable:.6f}. Überspringe.")
                 continue

            try:
                 # Trigger leicht unter dem Limit-Preis
                 trigger_delta_pct = strategy_params.get('trigger_price_delta_pct', 0.05) / 100.0
                 entry_trigger_price = entry_limit_price * (1 - trigger_delta_pct)

                 # Entry Order (Trigger Limit)
                 exchange.place_trigger_limit_order(
                     symbol=symbol, side='sell', amount=amount,
                     trigger_price=entry_trigger_price, price=entry_limit_price
                 )
                 logger.info(f"Short Entry {i+1}/{num_envelopes} platziert: Amount={amount:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")


                 # Zugehöriger Take Profit (Trigger Market am Average)
                 tp_price = band_prices['average']
                 exchange.place_trigger_market_order(
                     symbol=symbol, side='buy', amount=amount,
                     trigger_price=tp_price, reduce=True
                 )
                 logger.debug(f"  TP für Short Entry {i+1} @ {tp_price:.4f} platziert.")


                 # Zugehöriger Stop Loss (Trigger Market)
                 sl_pct = risk_params['stop_loss_pct'] / 100.0
                 sl_price = entry_limit_price * (1 + sl_pct) # SL basiert auf dem geplanten Entry
                 sl_order = exchange.place_trigger_market_order(
                     symbol=symbol, side='buy', amount=amount,
                     trigger_price=sl_price, reduce=True
                 )
                 logger.debug(f"  SL für Short Entry {i+1} @ {sl_price:.4f} platziert.")
                 if sl_order and 'id' in sl_order:
                     new_sl_ids.append(sl_order['id'])

            except Exception as e:
                logger.error(f"Fehler beim Platzieren der Short-Order-Gruppe {i+1}: {e}", exc_info=True)


    # Tracker mit neuen SL IDs aktualisieren
    tracker_info = read_tracker_file(tracker_file_path) # Erneut lesen
    tracker_info["stop_loss_ids"] = new_sl_ids
    update_tracker_file(tracker_file_path, tracker_info)


def full_trade_cycle(exchange, params, telegram_config, logger):
    """Der Haupt-Handelszyklus für eine einzelne Envelope-Strategie."""
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    tracker_file_path = get_tracker_file_path(symbol, timeframe)
    account_name = exchange.account.get('name', 'Standard-Account')

    try:
        # --- 1. Daten holen und Indikatoren berechnen ---
        # Genug Daten für den längsten Indikator holen (z.B. 100 Kerzen)
        data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=100)
        if data.empty or len(data) < params['strategy']['average_period']:
            logger.warning(f"Nicht genügend Daten für {symbol} ({timeframe}) erhalten. Überspringe Zyklus.")
            return

        # Berechne Indikatoren und die relevanten Preise für die letzte Kerze
        # data_with_indicators, band_prices = calculate_indicators_and_signals(data.iloc[:-1], params) # Nutze abgeschlossene Kerzen
        data_with_indicators, band_prices = calculate_indicators_and_signals(data, params) # Nutze aktuellste Daten für Preise

        if band_prices['average'] is None:
             logger.warning("Konnte Indikatoren nicht berechnen (evtl. zu wenig Daten nach dropna). Überspringe.")
             return

        last_price = data['close'].iloc[-1]
        logger.info(f"Aktueller Status für {symbol}: Preis={last_price:.4f}, Average={band_prices['average']:.4f}")

        # --- 2. Prüfen, ob SL ausgelöst wurde ---
        # Muss vor dem Stornieren der Orders passieren, um die ID zu erkennen
        sl_triggered = check_stop_loss_trigger(exchange, symbol, tracker_file_path, logger)

        # --- 3. Alle alten Orders der Strategie stornieren ---
        cancel_strategy_orders(exchange, symbol, logger)

        # --- 4. Tracker-Status prüfen ("Cooldown" nach SL) ---
        tracker_info = read_tracker_file(tracker_file_path)
        logger.info(f"Tracker-Status: {tracker_info['status']}, Letzte Seite: {tracker_info['last_side']}")

        if tracker_info['status'] != "ok_to_trade":
            # Prüfen, ob der Preis den Average gekreuzt hat, um Cooldown zu beenden
            if (tracker_info['last_side'] == 'long' and last_price >= band_prices['average']) or \
               (tracker_info['last_side'] == 'short' and last_price <= band_prices['average']):
                logger.info(f"Preis hat Average gekreuzt. Setze Status zurück auf 'ok_to_trade'.")
                tracker_info['status'] = 'ok_to_trade'
                update_tracker_file(tracker_file_path, tracker_info)
            else:
                logger.info(f"Bot ist im Cooldown-Modus ('{tracker_info['status']}'). Keine neuen Entries bis Preis Average kreuzt.")
                # Wichtig: Trotz Cooldown muss eine evtl. offene Position gemanagt werden!
                position = exchange.fetch_open_positions(symbol)
                position = position[0] if position else None
                if position:
                     manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger)
                return # Keine neuen Entries im Cooldown

        # --- 5. Offene Position prüfen und verwalten ---
        position = exchange.fetch_open_positions(symbol)
        position = position[0] if position else None

        if position:
            # Position ist offen -> TP/SL aktualisieren
            manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger)
            # Keine neuen Entry-Orders platzieren, wenn schon eine Position offen ist
            logger.info(f"Position für {symbol} ist offen. Nur TP/SL verwaltet.")
        else:
            # Keine Position offen -> Neue Entry Orders platzieren
            logger.info(f"Keine offene Position für {symbol} gefunden.")
            if sl_triggered:
                # Obwohl der Status jetzt wieder "ok_to_trade" sein KÖNNTE (wenn Preis Average gekreuzt hat),
                # wollen wir direkt nach einem SL-Trigger nicht sofort wieder einsteigen.
                logger.warning("SL wurde in diesem Zyklus ausgelöst. Überspringe Platzierung neuer Entry-Orders.")
                # Tracker Status wurde schon in check_stop_loss_trigger gesetzt
                return

            # Hole aktuellen Kontostand
            current_balance = exchange.fetch_balance_usdt()
            if current_balance <= 0:
                logger.error("Kein Guthaben zum Platzieren von Entry-Orders vorhanden.")
                return

            # Setze Margin Mode und Leverage (nur wenn keine Position offen ist)
            try:
                risk_params = params['risk']
                exchange.set_margin_mode(symbol, margin_mode=risk_params['margin_mode'])
                exchange.set_leverage(symbol, margin_mode=risk_params['margin_mode'], leverage=risk_params['leverage'])
            except Exception as e:
                 # Warnung loggen, aber weitermachen (vielleicht ist es schon richtig gesetzt)
                 logger.warning(f"Konnte Margin Mode/Leverage nicht setzen (evtl. schon korrekt?): {e}")


            place_entry_orders(exchange, band_prices, params, current_balance, tracker_file_path, logger)

    except ccxt.InsufficientFunds as e:
        logger.error(f"Fehler: Nicht genügend Guthaben für {symbol}. {e}")
        # Evtl. Telegram-Nachricht senden
        message = f"🚨 *Guthabenfehler* bei {account_name} ({symbol}):\nNicht genügend Guthaben für die Aktion."
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)

    except ccxt.NetworkError as e:
         logger.error(f"Netzwerkfehler bei der Kommunikation mit der Börse für {symbol}: {e}")
         # Hier nicht abbrechen, nächster Lauf könnte funktionieren

    except ccxt.ExchangeError as e:
         logger.error(f"Börsenfehler für {symbol}: {e}", exc_info=True)
         # Potenziell kritisch, Nachricht senden
         message = f"⚠️ *Börsenfehler* bei {account_name} ({symbol}):\n`{e}`"
         send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)

    except Exception as e:
        logger.critical(f"Unerwarteter kritischer Fehler im Handelszyklus für {symbol}: {e}", exc_info=True)
        # Kritische Nachricht senden über Guardian (wird automatisch gemacht, wenn `run_for_account` dekoriert ist)
        # Hier könnten wir zusätzlich eine spezifischere Nachricht senden:
        message = f"💥 *Kritischer Fehler* im Trade Cycle für {account_name} ({symbol}):\n`{e}`"
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
        # Wichtig: Den Fehler weiter werfen, damit der Guardian ihn fangen kann
        raise e
