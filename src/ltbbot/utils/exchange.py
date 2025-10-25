# src/ltbbot/utils/exchange.py
import ccxt
import pandas as pd
from datetime import datetime, timezone
import logging
import time # Hinzugefügt für fetch_recent_ohlcv

logger = logging.getLogger(__name__)

class Exchange:
    def __init__(self, account_config):
        self.account = account_config
        # Wichtig: defaultType auf 'swap' setzen für Perpetual Futures bei Bitget
        self.exchange = getattr(ccxt, 'bitget')({
            'apiKey': self.account.get('apiKey'),
            'secret': self.account.get('secret'),
            'password': self.account.get('password'),
            'options': {
                'defaultType': 'swap', # Korrekt für Perpetual Futures
                # 'productType': 'USDT-FUTURES' # Kann oft weggelassen werden, wenn defaultType='swap'
            },
        })
        # Optional: Sandbox Modus, falls unterstützt und gewünscht
        # try:
        #     self.exchange.set_sandbox_mode(True)
        # except NotSupported:
        #     logger.warning("Sandbox mode not supported by this exchange via ccxt.")
        # except Exception as e:
        #     logger.warning(f"Could not set sandbox mode: {e}")

        try:
            self.markets = self.exchange.load_markets()
            logger.info("Märkte erfolgreich geladen.")
        except Exception as e:
            logger.critical(f"Konnte Märkte nicht laden! API-Keys oder Verbindung prüfen. Fehler: {e}")
            self.markets = {} # Leeres Dict, um spätere Fehler zu vermeiden

    # --- OHLCV Methoden (angepasst aus deinem bitget_futures.py) ---
    def fetch_recent_ohlcv(self, symbol, timeframe, limit=1000):
        # Bitget's fetch_ohlcv hat oft ein Limit von 100 oder 200 pro Request.
        # Diese Methode versucht, das zu umgehen, indem sie mehrere Anfragen stellt.
        if not self.exchange.has['fetchOHLCV']:
            raise ccxt.NotSupported("fetchOHLCV not supported by the exchange.")

        # Zeitrahmen in Millisekunden umrechnen
        timeframe_duration_in_ms = self.exchange.parse_timeframe(timeframe) * 1000

        # Berechne den Startzeitpunkt basierend auf Limit und Zeitrahmen
        since = self.exchange.milliseconds() - timeframe_duration_in_ms * limit

        all_ohlcv = []
        fetch_limit = 200 # Typisches Limit pro Bitget API Call

        while since < self.exchange.milliseconds():
             try:
                 # logger.debug(f"Fetching OHLCV for {symbol} since {self.exchange.iso8601(since)} with limit {fetch_limit}")
                 ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, since, fetch_limit)
                 if not ohlcv:
                     # logger.debug("No more data received.")
                     break
                 all_ohlcv.extend(ohlcv)
                 since = ohlcv[-1][0] + timeframe_duration_in_ms # Setze 'since' auf den Timestamp der nächsten Kerze
                 # Kleine Pause, um Rate Limits zu vermeiden
                 # time.sleep(self.exchange.rateLimit / 1000)
             except ccxt.RateLimitExceeded as e:
                  logger.warning(f"Rate limit exceeded: {e}. Waiting...")
                  time.sleep(5) # Warte länger bei Rate Limit Fehler
             except Exception as e:
                  logger.error(f"Error fetching OHLCV chunk for {symbol}: {e}")
                  # Hier könnte man entscheiden, ob man abbricht oder es erneut versucht
                  time.sleep(1) # Kurze Pause nach anderem Fehler
                  break # Bei anderen Fehlern erstmal abbrechen

        if not all_ohlcv:
             logger.warning(f"No OHLCV data fetched for {symbol} ({timeframe}).")
             return pd.DataFrame()

        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        # Duplikate entfernen (kann durch überlappende Anfragen entstehen)
        df = df[~df.index.duplicated(keep='last')]

        # Stelle sicher, dass wir nicht mehr als 'limit' Kerzen zurückgeben
        if len(df) > limit:
            df = df.iloc[-limit:]

        # logger.info(f"Fetched {len(df)} unique candles for {symbol} ({timeframe}).")
        return df


    def fetch_historical_ohlcv(self, symbol, timeframe, start_date_str, end_date_str):
        if not self.exchange.has['fetchOHLCV']:
            raise ccxt.NotSupported("fetchOHLCV not supported by the exchange.")

        start_ts = int(self.exchange.parse8601(start_date_str + 'T00:00:00Z'))
        end_ts = int(self.exchange.parse8601(end_date_str + 'T23:59:59Z')) # Ende des Tages einschließen
        timeframe_duration_in_ms = self.exchange.parse_timeframe(timeframe) * 1000
        all_ohlcv = []
        fetch_limit = 200 # Typisches Bitget Limit

        current_ts = start_ts
        logger.info(f"Starte historischen Download für {symbol} ({timeframe}) von {start_date_str} bis {end_date_str}")

        while current_ts < end_ts:
            try:
                # logger.debug(f"Fetching historical OHLCV since {self.exchange.iso8601(current_ts)}")
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, current_ts, fetch_limit)
                if not ohlcv:
                    logger.debug("Keine weiteren historischen Daten empfangen.")
                    break

                # Filtern, um sicherzustellen, dass wir nicht über end_ts hinausgehen
                ohlcv = [candle for candle in ohlcv if candle[0] <= end_ts]
                if not ohlcv:
                    break

                all_ohlcv.extend(ohlcv)
                current_ts = ohlcv[-1][0] + timeframe_duration_in_ms # Nächste Kerze anfordern

                # Fortschritt loggen (optional)
                last_date = pd.to_datetime(ohlcv[-1][0], unit='ms', utc=True).strftime('%Y-%m-%d')
                logger.info(f"Daten bis {last_date} heruntergeladen...")

                # Rate Limit beachten
                time.sleep(self.exchange.rateLimit / 1000)

            except ccxt.RateLimitExceeded as e:
                logger.warning(f"Rate limit exceeded during historical fetch: {e}. Waiting...")
                time.sleep(10) # Länger warten bei historischen Daten
            except Exception as e:
                logger.error(f"Error fetching historical OHLCV chunk: {e}")
                time.sleep(5) # Kurze Pause und weitermachen oder abbrechen?

        if not all_ohlcv:
            logger.warning(f"Keine historischen OHLCV-Daten für {symbol} im Zeitraum gefunden.")
            return pd.DataFrame()

        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')] # Duplikate entfernen
        # Filtere den DataFrame genau auf den angeforderten Zeitraum
        df = df[(df.index >= pd.to_datetime(start_date_str, utc=True)) & (df.index <= pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True))]

        logger.info(f"Historischer Download abgeschlossen. {len(df)} Kerzen geladen.")
        return df

    # --- Getter Methoden ---
    def fetch_ticker(self, symbol):
        try:
             return self.exchange.fetch_ticker(symbol)
        except Exception as e:
             logger.error(f"Fehler beim Abrufen des Tickers für {symbol}: {e}")
             raise e # Fehler weitergeben

    def fetch_min_amount_tradable(self, symbol: str) -> float:
        try:
            # Stelle sicher, dass Märkte geladen wurden
            if not self.markets or symbol not in self.markets:
                logger.warning(f"Markt {symbol} nicht gefunden oder Märkte nicht geladen. Lade erneut...")
                self.markets = self.exchange.load_markets()
                if not self.markets or symbol not in self.markets:
                     logger.error(f"Markt {symbol} konnte auch nach erneutem Laden nicht gefunden werden.")
                     return 0.0 # Fallback

            min_amount = self.markets[symbol].get('limits', {}).get('amount', {}).get('min')
            if min_amount is None:
                 logger.warning(f"Minimale Handelsmenge für {symbol} nicht gefunden. Verwende Fallback 0.0")
                 return 0.0
            return float(min_amount)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der minimalen Handelsmenge für {symbol}: {e}")
            return 0.0 # Sicherer Fallback


    def amount_to_precision(self, symbol: str, amount: float) -> str:
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception as e:
            logger.error(f"Fehler beim Konvertieren der Menge {amount} für {symbol}: {e}")
            # Fallback: Versuche, ohne Präzision zu arbeiten (riskant)
            return str(amount)


    def price_to_precision(self, symbol: str, price: float) -> str:
        try:
            return self.exchange.price_to_precision(symbol, price)
        except Exception as e:
            logger.error(f"Fehler beim Konvertieren des Preises {price} für {symbol}: {e}")
            # Fallback: Versuche, ohne Präzision zu arbeiten (riskant)
            return str(price)

    def fetch_balance_usdt(self):
        try:
            # Parameter für Futures-Balance bei Bitget (kann je nach API-Version variieren)
            params = {'marginCoin': 'USDT'}
            balance = self.exchange.fetch_balance(params=params)

            # Suche nach USDT in der Balance-Struktur
            # Bitget Futures Balance ist oft unter 'info' oder direkt im Hauptobjekt
            usdt_balance = 0.0

            if 'USDT' in balance:
                 # Standard ccxt Struktur
                 usdt_balance = float(balance['USDT'].get('free', 0.0))
            elif 'info' in balance and isinstance(balance['info'], list):
                 # Bitget spezifische Struktur (oft eine Liste von Assets)
                 for asset_info in balance['info']:
                     if asset_info.get('marginCoin') == 'USDT':
                         # Suche nach 'available' oder 'equity' oder ähnlichem
                         # 'available' ist oft der frei verfügbare Betrag
                         usdt_balance = float(asset_info.get('available', 0.0))
                         break
            elif 'info' in balance and isinstance(balance['info'], dict):
                 # Alternative Bitget Struktur (manchmal ein Dict)
                 if 'USDT' in balance['info']:
                     usdt_balance = float(balance['info']['USDT'].get('available', 0.0))

            if usdt_balance == 0.0 and 'total' in balance and 'USDT' in balance['total']:
                 # Fallback auf 'total' wenn 'free' nicht verfügbar ist
                 usdt_balance = float(balance['total']['USDT'])


            # logger.debug(f"Abgerufene Balance: {balance}") # Zum Debuggen
            logger.info(f"Verfügbares USDT-Guthaben: {usdt_balance:.2f}")
            return usdt_balance

        except ccxt.AuthenticationError as e:
             logger.critical(f"Authentifizierungsfehler beim Abrufen des Kontostands: {e}. API-Keys prüfen!")
             return 0.0
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Kontostands: {e}", exc_info=True)
            return 0.0

    # --- Order & Positions Management Methoden (angepasst aus deinem bitget_futures.py) ---

    def fetch_order(self, id: str, symbol: str):
        try:
            # Bitget benötigt manchmal zusätzliche Parameter für Futures
            params = {'productType': 'USDT-FUTURES'} # Beispiel, kann variieren
            return self.exchange.fetch_order(id, symbol, params=params)
        except ccxt.OrderNotFound:
             logger.warning(f"Order {id} für {symbol} nicht gefunden.")
             return None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen von Order {id} für {symbol}: {e}")
            raise e

    def fetch_open_orders(self, symbol: str):
        try:
            # Normale (nicht-Trigger) Orders
            params = {'stop': False, 'productType': 'USDT-FUTURES'} # Explizit 'stop': False
            return self.exchange.fetch_open_orders(symbol, params=params)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen offener Orders für {symbol}: {e}")
            return [] # Leere Liste bei Fehler

    def fetch_open_trigger_orders(self, symbol: str):
        try:
            # Nur Trigger Orders
            params = {'stop': True, 'productType': 'USDT-FUTURES'} # Explizit 'stop': True
            return self.exchange.fetch_open_orders(symbol, params=params)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen offener Trigger-Orders für {symbol}: {e}")
            return []

    def fetch_closed_trigger_orders(self, symbol: str, limit: int = 20):
        try:
            # Geschlossene Trigger Orders
             params = {'stop': True, 'productType': 'USDT-FUTURES'}
             # 'fetchClosedOrders' ist nicht immer für Trigger verfügbar, 'fetchOrders' ist oft besser
             if self.exchange.has['fetchOrders']:
                 # Filtere nach Status ('closed', 'canceled') und Typ
                 all_orders = self.exchange.fetch_orders(symbol, limit=limit*2, params=params) # Mehr holen zum Filtern
                 closed_triggers = [
                     o for o in all_orders
                     if o.get('stopPrice') is not None and o['status'] in ['closed', 'canceled']
                 ]
                 # Sortiere nach Zeitstempel (neueste zuerst) und nimm das Limit
                 closed_triggers.sort(key=lambda x: x['timestamp'], reverse=True)
                 return closed_triggers[:limit]
             else:
                 logger.warning("fetchOrders nicht unterstützt, versuche fetchClosedOrders für Trigger.")
                 # Fallback (könnte leer sein oder nicht funktionieren)
                 return self.exchange.fetch_closed_orders(symbol, limit=limit, params=params)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen geschlossener Trigger-Orders für {symbol}: {e}")
            return []


    def cancel_order(self, id: str, symbol: str):
        try:
             params = {'stop': False} # Normale Order
             return self.exchange.cancel_order(id, symbol, params=params)
        except ccxt.OrderNotFound:
             logger.warning(f"Normale Order {id} für {symbol} beim Stornieren nicht gefunden.")
             return None # Oder leeres Dict zurückgeben?
        except Exception as e:
            logger.error(f"Fehler beim Stornieren der normalen Order {id} für {symbol}: {e}")
            raise e


    def cancel_trigger_order(self, id: str, symbol: str):
        try:
            params = {'stop': True} # Trigger Order
            return self.exchange.cancel_order(id, symbol, params=params)
        except ccxt.OrderNotFound:
             logger.warning(f"Trigger Order {id} für {symbol} beim Stornieren nicht gefunden.")
             return None
        except Exception as e:
            logger.error(f"Fehler beim Stornieren der Trigger Order {id} für {symbol}: {e}")
            raise e

    def fetch_open_positions(self, symbol):
        try:
             # Parameter für Bitget USDT Futures
             params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
             # fetch_positions benötigt oft eine Liste von Symbolen
             positions = self.exchange.fetch_positions([symbol], params=params)
             # Filtere nach Positionen mit tatsächlicher Größe (> 0)
             open_positions = [p for p in positions if p.get('contracts') is not None and float(p['contracts']) != 0.0]
             # logger.debug(f"Gefundene offene Positionen für {symbol}: {open_positions}")
             return open_positions
        except Exception as e:
            logger.error(f"Fehler beim Abrufen offener Positionen für {symbol}: {e}", exc_info=True)
            return []


    def close_position(self, symbol: str, side: Optional[str] = None):
        """Schließt eine Position über eine Market Order."""
        try:
             position = self.fetch_open_positions(symbol)
             if not position:
                 logger.warning(f"Keine offene Position zum Schließen für {symbol} gefunden.")
                 return None
             position = position[0] # Nimm die erste gefundene Position

             close_side = 'sell' if position['side'] == 'long' else 'buy'
             amount = position['contracts'] # Menge in Kontrakten

             logger.info(f"Schließe {position['side']} Position für {symbol} mit Market Order (Menge: {amount}).")
             return self.place_market_order(symbol, close_side, amount, reduce=True)

        except Exception as e:
            logger.error(f"Fehler beim Schließen der Position für {symbol}: {e}")
            raise e


    def set_margin_mode(self, symbol, margin_mode='isolated'):
        # Sicherstellen, dass der Modus 'isolated' oder 'cross' ist
        margin_mode_lower = margin_mode.lower()
        if margin_mode_lower not in ['isolated', 'cross']:
            logger.error(f"Ungültiger Margin-Modus: {margin_mode}. Muss 'isolated' oder 'cross' sein.")
            return

        try:
            # Parameter für Bitget
            params={'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            # Wichtig: Symbol zuerst, dann der Modus
            self.exchange.set_margin_mode(margin_mode_lower, symbol, params=params)
            logger.info(f"Margin-Modus für {symbol} auf '{margin_mode_lower}' gesetzt.")
        except ccxt.NotSupported as e:
             logger.warning(f"Setzen des Margin-Modus wird von ccxt für Bitget möglicherweise nicht direkt unterstützt oder erfordert spezielle Params: {e}")
        except ccxt.ExchangeError as e:
             # Ignoriere Fehler, wenn Modus bereits gesetzt ist
             if 'Margin mode is the same' in str(e) or 'margin mode is not changed' in str(e).lower():
                 logger.debug(f"Margin-Modus für {symbol} ist bereits '{margin_mode_lower}'.")
             else:
                 logger.error(f"Fehler beim Setzen des Margin-Modus für {symbol} auf '{margin_mode_lower}': {e}")
                 # raise e # Optional: Fehler weitergeben
        except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Setzen des Margin-Modus für {symbol}: {e}")
            # raise e # Optional: Fehler weitergeben

    def set_leverage(self, symbol, leverage, margin_mode='isolated'):
         try:
             leverage = int(leverage) # Muss Integer sein
             params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}

             if margin_mode.lower() == 'isolated':
                 # Für Isolated muss der Hebel für Long und Short separat gesetzt werden
                 params_long = params.copy()
                 params_long['holdSide'] = 'long'
                 self.exchange.set_leverage(leverage, symbol, params=params_long)
                 logger.debug(f"Isolated Leverage für {symbol} (Long) auf {leverage}x gesetzt.")

                 params_short = params.copy()
                 params_short['holdSide'] = 'short'
                 self.exchange.set_leverage(leverage, symbol, params=params_short)
                 logger.debug(f"Isolated Leverage für {symbol} (Short) auf {leverage}x gesetzt.")
             else: # Cross
                 self.exchange.set_leverage(leverage, symbol, params=params)
                 logger.debug(f"Cross Leverage für {symbol} auf {leverage}x gesetzt.")

             logger.info(f"Hebel für {symbol} ({margin_mode}) auf {leverage}x gesetzt.")

         except ccxt.ExchangeError as e:
             # Ignoriere Fehler, wenn Hebel nicht geändert wurde
             if 'Leverage not changed' in str(e) or 'leverage is not modified' in str(e).lower():
                 logger.debug(f"Hebel für {symbol} ist bereits {leverage}x.")
             else:
                 logger.error(f"Fehler beim Setzen des Hebels für {symbol} auf {leverage}x: {e}")
                 # raise e
         except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Setzen des Hebels für {symbol}: {e}")
            # raise e

    # --- Order Platzierungs Methoden (angepasst aus deinem bitget_futures.py) ---

    def place_market_order(self, symbol: str, side: str, amount: float, reduce: bool = False):
        """Platziert eine Market Order."""
        try:
            params = {'reduceOnly': reduce}
            # Konvertiere amount in String-Darstellung mit der korrekten Präzision
            amount_str = self.amount_to_precision(symbol, amount)
            logger.info(f"Platziere Market Order: {side.upper()} {amount_str} {symbol} (Reduce: {reduce})")
            return self.exchange.create_order(symbol, 'market', side, float(amount_str), params=params)
        except ccxt.InsufficientFunds as e:
             logger.error(f"Nicht genügend Guthaben für Market Order {side} {amount} {symbol}: {e}")
             raise e # Wichtig, damit der trade_manager den Fehler behandeln kann
        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Market Order für {symbol}: {e}", exc_info=True)
            raise e

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float, reduce: bool = False):
        """Platziert eine Limit Order."""
        try:
            params = {'reduceOnly': reduce}
            amount_str = self.amount_to_precision(symbol, amount)
            price_str = self.price_to_precision(symbol, price)
            logger.info(f"Platziere Limit Order: {side.upper()} {amount_str} {symbol} @ {price_str} (Reduce: {reduce})")
            return self.exchange.create_order(symbol, 'limit', side, float(amount_str), float(price_str), params=params)
        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Limit Order für {symbol}: {e}", exc_info=True)
            raise e


    def place_trigger_market_order(self, symbol: str, side: str, amount: float, trigger_price: float, reduce: bool = False):
        """Platziert eine Trigger-Market Order (Stop Market oder Take Profit Market)."""
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            trigger_price_str = self.price_to_precision(symbol, trigger_price)

            # Bitget spezifische Parameter für Trigger Orders
            params = {
                'stopPrice': trigger_price_str, # Der Auslösepreis
                'triggerType': 'market_price', # oder 'fill_price', je nach gewünschtem Trigger
                'planType': 'normal_plan', # Für einfache SL/TP
                'reduceOnly': reduce,
                # 'tradeSide': 'close' # wird oft impliziert durch reduceOnly
            }

            # Wichtig: create_order mit 'market' und stopPrice Parameter
            logger.info(f"Platziere Trigger Market Order: {side.upper()} {amount_str} {symbol}, Trigger @ {trigger_price_str} (Reduce: {reduce})")
            # ACHTUNG: ccxt Standard 'triggerPrice' funktioniert bei Bitget oft nicht direkt in create_order.
            # Wir verwenden stattdessen 'stopPrice' im params-Dict.
            # Der 'type' bleibt 'market', da es nach dem Trigger eine Market Order wird.
            order = self.exchange.create_order(symbol, 'market', side, float(amount_str), params=params)
            return order

        except ccxt.ExchangeError as e:
             # Spezifische Fehler behandeln, z.B. ungültiger Triggerpreis
             logger.error(f"Börsenfehler beim Platzieren der Trigger Market Order für {symbol}: {e}", exc_info=True)
             # Hier könnte man prüfen, ob der Fehler z.B. "trigger price is too close" ist
             raise e
        except Exception as e:
            logger.error(f"Allgemeiner Fehler beim Platzieren der Trigger Market Order für {symbol}: {e}", exc_info=True)
            raise e


    def place_trigger_limit_order(self, symbol: str, side: str, amount: float, trigger_price: float, price: float, reduce: bool = False):
        """Platziert eine Trigger-Limit Order."""
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            trigger_price_str = self.price_to_precision(symbol, trigger_price)
            price_str = self.price_to_precision(symbol, price) # Limit Preis nach Trigger

            # Bitget spezifische Parameter
            params = {
                'stopPrice': trigger_price_str,
                'triggerType': 'market_price', # oder 'fill_price'
                'planType': 'normal_plan',
                'reduceOnly': reduce,
            }

            # Wichtig: type ist 'limit', stopPrice im params dict
            logger.info(f"Platziere Trigger Limit Order: {side.upper()} {amount_str} {symbol}, Trigger @ {trigger_price_str}, Limit @ {price_str} (Reduce: {reduce})")
            order = self.exchange.create_order(symbol, 'limit', side, float(amount_str), float(price_str), params=params)
            return order
        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Trigger Limit Order für {symbol}: {e}", exc_info=True)
            raise e

    # --- Implizite Methoden (falls Trailing Stop benötigt wird) ---
    # Beachte: Diese sind nicht Teil des Standard-CCXT und können sich ändern!
    def place_trailing_stop_order(self, symbol, side, amount, activation_price, callback_rate_decimal, params={}):
         """
         Platziert eine Trailing Stop Market Order über die implizite API-Methode von Bitget.
         :param callback_rate_decimal: Die Callback-Rate als Dezimalzahl (z.B. 0.01 für 1%)
         """
         try:
             market_id = self.exchange.market(symbol)['id']
             margin_coin = 'USDT' # Annahme für :USDT Paare

             # API erwartet 'close_long' oder 'close_short' für SL/TP Orders
             if side.lower() == 'sell':
                 api_side = 'close_long'
             elif side.lower() == 'buy':
                 api_side = 'close_short'
             else:
                 raise ValueError(f"Ungültiger 'side' für Trailing Stop: {side}")

             # Callback Rate von Dezimal (0.01) in API-Prozent-String ("1.0") umwandeln
             api_callback_rate_str = str(callback_rate_decimal * 100.0)

             api_params = {
                 'symbol': market_id,
                 'marginCoin': margin_coin,
                 'planType': 'trailing_stop', # Spezifisch für Bitget TSL
                 'side': api_side,
                 'size': str(self.amount_to_precision(symbol, amount)), # Menge als String
                 'triggerPrice': str(self.price_to_precision(symbol, activation_price)), # Aktivierungspreis
                 'rangeRate': api_callback_rate_str, # Callback Rate als String %
                 # 'reduceOnly': params.get('reduceOnly', True) # reduceOnly ist bei TSL implizit? Sicherer ist, es explizit zu versuchen, wenn die API es erlaubt.
             }
             # Manchmal muss reduceOnly außerhalb des Haupt-Params-Dicts übergeben werden
             # extra_params = {'reduceOnly': params.get('reduceOnly', True)}


             logger.info(f"Sende impliziten TSL-Aufruf: private_mix_post_plan_place_plan mit Params: {api_params}")

             # Aufruf der impliziten Methode (kann sich mit ccxt Updates ändern!)
             # Der genaue Methodenname muss ggf. geprüft werden (z.B. in ccxt Code oder API Doku)
             # 'private_mix_post_plan_place_plan' ist eine häufige Variante
             response = self.exchange.private_mix_post_plan_place_plan(api_params) # Oder nur self.exchange.privatePost...

             logger.info(f"TSL-Antwort von Bitget API: {response}")
             # Parse die Antwort, um eine ccxt-ähnliche Order-Struktur zurückzugeben (optional)
             # order_id = response.get('data', {}).get('orderId')
             # return {'id': order_id, 'info': response}
             return response # Gib die Roh-Antwort zurück

         except AttributeError:
              logger.error("Implizite Methode für Trailing Stop nicht gefunden. CCXT Version oder Bitget API prüfen.")
              raise ccxt.NotSupported("Trailing Stop via impliziter Methode nicht verfügbar.")
         except Exception as e:
             logger.error(f"Kritischer Fehler beim Aufruf der impliziten TSL-Methode: {e}", exc_info=True)
             raise e # Fehler weitergeben für Fallback (z.B. fixer SL)
