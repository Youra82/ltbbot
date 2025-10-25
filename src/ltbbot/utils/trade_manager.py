# src/ltbbot/utils/trade_manager.py
import logging
import time
import ccxt
import os
import json
from datetime import datetime
import sys

# Pfade für die Tracker-Datei definieren
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TRACKER_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker')

sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.telegram import send_message
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals


def get_tracker_file_path(symbol, timeframe):
    """Generiert den Pfad zur Tracker-Datei für eine Strategie."""
    safe_filename = f"{symbol.replace('/', '-').replace(':', '-')}_{timeframe}.json"
    return os.path.join(TRACKER_DIR, safe_filename)

def read_tracker_file(file_path):
    """Liest den Status aus der Tracker-Datei."""
    if not os.path.exists(file_path):
        default_data = {"status": "ok_to_trade", "last_side": None, "stop_loss_ids": []}
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        try: # Versuch, Standard zu schreiben
            with open(file_path, 'w') as f:
                json.dump(default_data, f, indent=4)
        except Exception as write_err:
             logging.error(f"Konnte initiale Tracker-Datei nicht schreiben: {write_err}")
        return default_data
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        logging.error(f"Fehler beim Lesen der Tracker-Datei {file_path}. Setze auf Standard zurück.")
        default_data = {"status": "ok_to_trade", "last_side": None, "stop_loss_ids": []}
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
            except ccxt.OrderNotFound: pass
            except Exception as e: logger.warning(f"Konnte normale Order {order['id']} nicht stornieren: {e}")

        # Trigger Orders (Entry, TP, SL)
        trigger_orders = exchange.fetch_open_trigger_orders(symbol)
        for order in trigger_orders:
            try:
                exchange.cancel_trigger_order(order['id'], symbol)
                cancelled_count += 1
                logger.debug(f"Trigger Order {order['id']} storniert.")
            except ccxt.OrderNotFound: pass
            except Exception as e: logger.warning(f"Konnte Trigger Order {order['id']} nicht stornieren: {e}")

        if cancelled_count > 0:
            logger.info(f"{cancelled_count} offene Order(s) für {symbol} storniert.")
        else:
            logger.debug(f"Keine offenen Orders für {symbol} zum Stornieren gefunden.") # Info zu Debug geändert
        return cancelled_count
    except Exception as e:
        logger.error(f"Fehler beim Stornieren der Orders für {symbol}: {e}", exc_info=True)
        return cancelled_count


def check_stop_loss_trigger(exchange, symbol, tracker_file_path, logger):
    """Prüft, ob ein von dieser Strategie gesetzter SL ausgelöst wurde."""
    tracker_info = read_tracker_file(tracker_file_path)
    current_sl_ids = tracker_info.get("stop_loss_ids", [])
    if not current_sl_ids:
        return False # Kein SL war gesetzt

    try:
        # Hole die letzten ~5 geschlossenen Trigger Orders
        closed_triggers = exchange.fetch_closed_trigger_orders(symbol, limit=5)
        if not closed_triggers:
            return False

        triggered_sl_found = False
        triggered_order_id = None
        pos_side = None # Seite der Position, die geschlossen wurde

        for closed_order in closed_triggers:
            if closed_order['id'] in current_sl_ids:
                 # Ein bekannter SL wurde geschlossen (ausgelöst oder manuell storniert)
                 # Wir nehmen an, dass 'closed' bedeutet, er wurde ausgelöst
                 if closed_order.get('status') == 'closed':
                     logger.warning(f"STOP LOSS wurde für {symbol} ausgelöst! Order ID: {closed_order['id']}")
                     triggered_sl_found = True
                     triggered_order_id = closed_order['id']
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
            # Wenn keiner der bekannten SLs geschlossen wurde, prüfe, ob sie noch offen sind
            # Es könnte sein, dass die fetch_closed_trigger_orders noch nicht aktuell ist
            open_triggers = exchange.fetch_open_trigger_orders(symbol)
            open_trigger_ids = {o['id'] for o in open_triggers}
            still_open_sl_ids = [sl_id for sl_id in current_sl_ids if sl_id in open_trigger_ids]

            # Wenn die Liste der offenen SLs von der im Tracker abweicht, aktualisiere den Tracker
            if set(still_open_sl_ids) != set(current_sl_ids):
                 logger.info("Aktualisiere SL IDs im Tracker, da einige nicht mehr offen sind.")
                 tracker_info["stop_loss_ids"] = still_open_sl_ids
                 update_tracker_file(tracker_file_path, tracker_info)
            return False # Kein SL wurde definitiv ausgelöst

    except Exception as e:
        logger.error(f"Fehler beim Prüfen geschlossener SL-Orders für {symbol}: {e}")
        return False


def manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger):
    """Verwaltet eine bestehende Position: Aktualisiert TP und SL."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    logger.info(f"Verwalte bestehende {position['side']}-Position für {symbol}.")

    # 1. Alte TP/SL Orders stornieren (nur die, die zum Schließen da sind)
    # cancel_strategy_orders erledigt dies bereits zu Beginn von full_trade_cycle

    # 2. Neuen TP (am Moving Average) und SL setzen
    amount_contracts = position['contracts'] # Menge in Kontrakten (String oder Float?)
    try: # Sicherstellen, dass es Float ist
        amount_contracts_float = float(amount_contracts)
    except ValueError:
        logger.error(f"Konnte Positionsgröße ('{amount_contracts}') nicht in Float umwandeln.")
        return

    new_sl_ids = []

    try:
        # Neuer Take Profit (immer am aktuellen Durchschnitt)
        tp_price = band_prices['average']
        tp_side = 'sell' if position['side'] == 'long' else 'buy'
        exchange.place_trigger_market_order(symbol, tp_side, amount_contracts_float, tp_price, reduce=True)
        logger.info(f"Neuen TP für {position['side']} @ {tp_price:.4f} gesetzt.")

        # Neuer Stop Loss (basierend auf ursprünglichem Entry und SL-Prozentsatz)
        avg_entry_price = float(position.get('entryPrice') or position.get('info', {}).get('openPriceAvg', 0))
        if avg_entry_price == 0:
            logger.error("Konnte Einstiegspreis für SL-Berechnung nicht ermitteln!")
            return

        sl_pct = risk_params['stop_loss_pct'] / 100.0
        if position['side'] == 'long':
            sl_price = avg_entry_price * (1 - sl_pct)
            sl_side = 'sell'
        else: # short
            sl_price = avg_entry_price * (1 + sl_pct)
            sl_side = 'buy'

        sl_order = exchange.place_trigger_market_order(symbol, sl_side, amount_contracts_float, sl_price, reduce=True)
        logger.info(f"Neuen SL für {position['side']} @ {sl_price:.4f} gesetzt.")
        if sl_order and 'id' in sl_order:
            new_sl_ids.append(sl_order['id'])

    except Exception as e:
        logger.error(f"Fehler beim Setzen von neuem TP/SL für {symbol}: {e}", exc_info=True)
        # Versuchen aufzuräumen (erneut canceln)
        cancel_strategy_orders(exchange, symbol, logger)

    # Tracker mit neuen SL IDs aktualisieren
    tracker_info = read_tracker_file(tracker_file_path)
    tracker_info["stop_loss_ids"] = new_sl_ids
    update_tracker_file(tracker_file_path, tracker_info)


def place_entry_orders(exchange, band_prices, params, balance, tracker_file_path, logger):
    """Platziert die gestaffelten Entry-, TP- und SL-Orders basierend auf Risiko."""
    symbol = params['market']['symbol']
    risk_params = params['risk']
    strategy_params = params['strategy']
    behavior_params = params['behavior']
    account_name = exchange.account.get('name', 'Standard-Account')

    logger.info(f"Platziere neue Entry-Orders für {symbol} (Risikobasierte Größe).")

    # Parameter holen
    leverage = risk_params['leverage']
    risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Standard 0.5% Risiko pro Layer
    stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0 # SL % aus Config
    num_envelopes = len(strategy_params['envelopes'])
    min_amount_tradable = exchange.fetch_min_amount_tradable(symbol)
    trigger_delta_pct_cfg = strategy_params.get('trigger_price_delta_pct', 0.05) / 100.0

    new_sl_ids = []

    # --- Long Orders ---
    if behavior_params.get('use_longs', True):
        side = 'buy'
        for i, entry_limit_price in enumerate(band_prices['long']):
            if entry_limit_price <= 0:
                logger.warning(f"Ungültiger Long-Entry-Preis ({entry_limit_price:.4f}) für Band {i+1}. Überspringe.")
                continue

            try:
                # 1. Risiko in USD berechnen
                risk_amount_usd = balance * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0:
                     logger.warning(f"Risk amount <= 0 ({risk_amount_usd:.2f}) für Layer {i+1}. Skipping.")
                     continue

                # 2. SL-Preis und Distanz berechnen
                entry_price_for_calc = entry_limit_price
                sl_price = entry_price_for_calc * (1 - stop_loss_pct_param)
                sl_distance_price = abs(entry_price_for_calc - sl_price)
                if sl_distance_price <= 0:
                     logger.warning(f"SL distance <= 0 for entry {entry_price_for_calc:.4f}. Skipping Layer {i+1}.")
                     continue

                # 3. Positionsgröße (amount_coins) berechnen
                amount_coins = risk_amount_usd / sl_distance_price

                # 4. Mindestmenge prüfen
                if amount_coins < min_amount_tradable:
                     logger.warning(f"Berechnete Long-Menge {amount_coins:.8f} für Layer {i+1} liegt unter Minimum {min_amount_tradable:.8f}. Überspringe.")
                     continue

                # 5. Benötigte Margin (nur zur Info)
                margin_required = (amount_coins * entry_price_for_calc) / leverage
                logger.debug(f"Long Layer {i+1}: Risk={risk_amount_usd:.2f}$, Size={amount_coins:.8f}, MarginReq={margin_required:.2f}$ (Balance: {balance:.2f})")
                # Echter Check passiert durch API-Call (InsufficientFunds)

                # Trigger leicht über dem Limit-Preis
                entry_trigger_price = entry_limit_price * (1 + trigger_delta_pct_cfg)

                # Entry Order (Trigger Limit)
                exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                logger.info(f"Long Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")

                # Zugehöriger Take Profit (Trigger Market am Average)
                tp_price = band_prices['average']
                exchange.place_trigger_market_order(
                    symbol=symbol, side='sell', amount=amount_coins,
                    trigger_price=tp_price, reduce=True
                )
                logger.debug(f"  TP für Long Entry {i+1} @ {tp_price:.4f} platziert.")

                # Zugehöriger Stop Loss (Trigger Market)
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='sell', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Long Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids.append(sl_order['id'])

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Long-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders.")
                break # Bei InsufficientFunds weitere Layer für diese Seite abbrechen
            except Exception as e:
                logger.error(f"Fehler beim Platzieren der Long-Order-Gruppe {i+1}: {e}", exc_info=True)
                # Nicht abbrechen, versuche nächsten Layer

    # --- Short Orders ---
    if behavior_params.get('use_shorts', True):
        side = 'sell'
        for i, entry_limit_price in enumerate(band_prices['short']):
            if entry_limit_price <= 0:
                logger.warning(f"Ungültiger Short-Entry-Preis ({entry_limit_price:.4f}) für Band {i+1}. Überspringe.")
                continue

            try:
                # 1. Risiko in USD berechnen
                risk_amount_usd = balance * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0:
                     logger.warning(f"Risk amount <= 0 ({risk_amount_usd:.2f}) für Layer {i+1}. Skipping.")
                     continue

                # 2. SL-Preis und Distanz berechnen
                entry_price_for_calc = entry_limit_price
                sl_price = entry_price_for_calc * (1 + stop_loss_pct_param)
                sl_distance_price = abs(entry_price_for_calc - sl_price)
                if sl_distance_price <= 0:
                     logger.warning(f"SL distance <= 0 for entry {entry_price_for_calc:.4f}. Skipping Layer {i+1}.")
                     continue

                # 3. Positionsgröße (amount_coins) berechnen
                amount_coins = risk_amount_usd / sl_distance_price

                # 4. Mindestmenge prüfen
                if amount_coins < min_amount_tradable:
                     logger.warning(f"Berechnete Short-Menge {amount_coins:.8f} für Layer {i+1} liegt unter Minimum {min_amount_tradable:.8f}. Überspringe.")
                     continue

                # 5. Benötigte Margin (nur zur Info)
                margin_required = (amount_coins * entry_price_for_calc) / leverage
                logger.debug(f"Short Layer {i+1}: Risk={risk_amount_usd:.2f}$, Size={amount_coins:.8f}, MarginReq={margin_required:.2f}$ (Balance: {balance:.2f})")

                # Trigger leicht unter dem Limit-Preis
                entry_trigger_price = entry_limit_price * (1 - trigger_delta_pct_cfg)

                # Entry Order (Trigger Limit)
                exchange.place_trigger_limit_order(
                    symbol=symbol, side=side, amount=amount_coins,
                    trigger_price=entry_trigger_price, price=entry_limit_price
                )
                logger.info(f"Short Entry {i+1}/{num_envelopes} platziert: Amount={amount_coins:.4f}, Trigger@{entry_trigger_price:.4f}, Limit@{entry_limit_price:.4f}")

                # Zugehöriger Take Profit (Trigger Market am Average)
                tp_price = band_prices['average']
                exchange.place_trigger_market_order(
                    symbol=symbol, side='buy', amount=amount_coins,
                    trigger_price=tp_price, reduce=True
                )
                logger.debug(f"  TP für Short Entry {i+1} @ {tp_price:.4f} platziert.")

                # Zugehöriger Stop Loss (Trigger Market)
                sl_order = exchange.place_trigger_market_order(
                    symbol=symbol, side='buy', amount=amount_coins,
                    trigger_price=sl_price, reduce=True
                )
                logger.debug(f"  SL für Short Entry {i+1} @ {sl_price:.4f} platziert.")
                if sl_order and 'id' in sl_order:
                    new_sl_ids.append(sl_order['id'])

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Short-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders.")
                break
            except Exception as e:
                logger.error(f"Fehler beim Platzieren der Short-Order-Gruppe {i+1}: {e}", exc_info=True)


    # Tracker mit neuen SL IDs aktualisieren (nur wenn Orders platziert wurden)
    if new_sl_ids:
        tracker_info = read_tracker_file(tracker_file_path)
        # Füge neue IDs hinzu, ohne alte zu löschen (falls manage_existing_position welche gesetzt hat)
        existing_ids = set(tracker_info.get("stop_loss_ids", []))
        all_sl_ids = list(existing_ids.union(set(new_sl_ids)))
        tracker_info["stop_loss_ids"] = all_sl_ids
        update_tracker_file(tracker_file_path, tracker_info)
        logger.info(f"Tracker mit {len(new_sl_ids)} neuen SL Order IDs aktualisiert.")
    elif not any(band_prices['long']) and not any(band_prices['short']): # Keine gültigen Preise gefunden
         logger.info("Keine gültigen Entry-Preise gefunden, keine Orders platziert.")
    else:
         logger.info("Keine Entry-Orders platziert (ggf. Menge zu klein oder Fehler).")


def full_trade_cycle(exchange, params, telegram_config, logger):
    """Der Haupt-Handelszyklus für eine einzelne Envelope-Strategie."""
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    tracker_file_path = get_tracker_file_path(symbol, timeframe)
    account_name = exchange.account.get('name', 'Standard-Account')

    try:
        # --- 1. Daten holen und Indikatoren berechnen ---
        data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=100)
        if data.empty or len(data) < params['strategy']['average_period']:
            logger.warning(f"Nicht genügend Daten für {symbol} ({timeframe}) erhalten. Überspringe Zyklus.")
            return

        data_with_indicators, band_prices = calculate_indicators_and_signals(data, params)

        if band_prices.get('average') is None or pd.isna(band_prices['average']):
            logger.warning(f"Konnte Indikatoren nicht berechnen für {symbol} (evtl. zu wenig Daten nach dropna). Überspringe.")
            return

        last_price = data['close'].iloc[-1]
        logger.info(f"Aktueller Status für {symbol}: Preis={last_price:.4f}, Average={band_prices['average']:.4f}")

        # --- 2. Prüfen, ob SL ausgelöst wurde ---
        sl_triggered_this_cycle = check_stop_loss_trigger(exchange, symbol, tracker_file_path, logger)

        # --- 3. Alle alten Orders der Strategie stornieren ---
        cancel_strategy_orders(exchange, symbol, logger)

        # --- 4. Tracker-Status prüfen ("Cooldown" nach SL) ---
        tracker_info = read_tracker_file(tracker_file_path)
        current_status = tracker_info['status']
        logger.info(f"Tracker-Status: {current_status}, Letzte Seite: {tracker_info['last_side']}")

        cooldown_active = False
        if current_status == "stop_loss_triggered":
            cooldown_active = True
            # Prüfen, ob Cooldown beendet werden kann
            if (tracker_info['last_side'] == 'long' and last_price >= band_prices['average']) or \
               (tracker_info['last_side'] == 'short' and last_price <= band_prices['average']):
                logger.info(f"Preis hat Average gekreuzt nach SL. Setze Status zurück auf 'ok_to_trade'.")
                tracker_info['status'] = 'ok_to_trade'
                tracker_info['last_side'] = None # Seite zurücksetzen
                update_tracker_file(tracker_file_path, tracker_info)
                cooldown_active = False # Cooldown für diesen Zyklus aufgehoben
            else:
                logger.info(f"Bot ist im Cooldown-Modus ('stop_loss_triggered'). Keine neuen Entries bis Preis Average kreuzt.")

        # --- 5. Offene Position prüfen und verwalten ---
        position = exchange.fetch_open_positions(symbol)
        position = position[0] if position else None

        if position:
            # Position ist offen -> TP/SL aktualisieren (auch im Cooldown!)
            manage_existing_position(exchange, position, band_prices, params, tracker_file_path, logger)
            logger.info(f"Position für {symbol} ist offen. Nur TP/SL verwaltet.")
            # Keine neuen Entry-Orders platzieren, wenn schon eine Position offen ist
        elif cooldown_active:
             logger.info("Keine Position offen, aber Cooldown aktiv. Keine neuen Entries.")
             # Stelle sicher, dass SL IDs im Tracker leer sind, falls manage_existing_position nicht lief
             if tracker_info.get("stop_loss_ids"):
                 tracker_info["stop_loss_ids"] = []
                 update_tracker_file(tracker_file_path, tracker_info)
        elif sl_triggered_this_cycle:
             # Direkt nach SL-Trigger in *diesem* Zyklus keine neuen Entries, auch wenn Cooldown formal aufgehoben wäre
             logger.warning("SL wurde in diesem Zyklus ausgelöst. Überspringe Platzierung neuer Entry-Orders für diesen Lauf.")
             # Tracker Status wurde schon in check_stop_loss_trigger gesetzt
        else:
             # Keine Position offen, kein Cooldown -> Neue Entry Orders platzieren
             logger.info(f"Keine offene Position für {symbol} und Cooldown nicht aktiv.")
             current_balance = exchange.fetch_balance_usdt()
             if current_balance <= 1: # Mindestguthaben (z.B. 1 USDT)
                 logger.error(f"Guthaben ({current_balance:.2f} USDT) zu gering zum Platzieren von Entry-Orders.")
                 return

             # Setze Margin Mode und Leverage
             try:
                 risk_params = params['risk']
                 exchange.set_margin_mode(symbol, margin_mode=risk_params['margin_mode'])
                 exchange.set_leverage(symbol, margin_mode=risk_params['margin_mode'], leverage=risk_params['leverage'])
             except Exception as e:
                 logger.warning(f"Konnte Margin Mode/Leverage nicht setzen (evtl. schon korrekt?): {e}")

             place_entry_orders(exchange, band_prices, params, current_balance, tracker_file_path, logger)

    except ccxt.InsufficientFunds as e:
        logger.error(f"Fehler: Nicht genügend Guthaben für {symbol}. {e}")
        message = f"🚨 *Guthabenfehler* bei {account_name} ({symbol}):\nNicht genügend Guthaben für die Aktion."
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)

    except ccxt.NetworkError as e:
        logger.error(f"Netzwerkfehler bei der Kommunikation mit der Börse für {symbol}: {e}")

    except ccxt.ExchangeError as e:
        logger.error(f"Börsenfehler für {symbol}: {e}", exc_info=False) # exc_info=False für weniger Output bei häufigen Fehlern
        # Potenziell kritisch, Nachricht senden
        message = f"⚠️ *Börsenfehler* bei {account_name} ({symbol}):\n`{e}`"
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)

    except Exception as e:
        logger.critical(f"Unerwarteter kritischer Fehler im Handelszyklus für {symbol}: {e}", exc_info=True)
        message = f"💥 *Kritischer Fehler* im Trade Cycle für {account_name} ({symbol}):\n`{e}`"
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), message)
        raise e # Fehler weiter werfen für Guardian
