# src/ltbbot/utils/exchange.py
# KORRIGIERTE VERSION (BASIEREND AUF JAEGERBOT/TITANBOT-LOGIK)
import ccxt
import pandas as pd
from datetime import datetime, timezone
import logging
import time # Hinzugefügt für fetch_recent_ohlcv
from typing import Optional, Dict, List, Any 
import os 
import sys 

# Pfad Setup für utils Import (ggf. anpassen)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

logger = logging.getLogger(__name__)

class Exchange:
    def __init__(self, account_config):
        self.account = account_config
        self.exchange = getattr(ccxt, 'bitget')({
            'apiKey': self.account.get('apiKey'),
            'secret': self.account.get('secret'),
            'password': self.account.get('password'),
            'options': {
                'defaultType': 'swap', 
            },
            'enableRateLimit': True, 
        })

        try:
            self.markets = self.exchange.load_markets()
            logger.info("Märkte erfolgreich geladen.")
        except Exception as e:
            logger.critical(f"Konnte Märkte nicht laden! API-Keys oder Verbindung prüfen. Fehler: {e}")
            self.markets = {} 

    # --- OHLCV Methoden ---
    def fetch_recent_ohlcv(self, symbol, timeframe, limit=1000):
        if not self.markets: return pd.DataFrame() 
        if not self.exchange.has['fetchOHLCV']:
            raise ccxt.NotSupported("fetchOHLCV not supported by the exchange.")

        timeframe_duration_in_ms = self.exchange.parse_timeframe(timeframe) * 1000
        since = self.exchange.milliseconds() - timeframe_duration_in_ms * limit
        all_ohlcv = []
        fetch_limit = 200 

        while since < self.exchange.milliseconds():
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, since, fetch_limit)
                if not ohlcv: break
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + timeframe_duration_in_ms
                time.sleep(self.exchange.rateLimit / 1000) 
            except ccxt.RateLimitExceeded as e:
                logger.warning(f"Rate limit exceeded: {e}. Waiting...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Error fetching OHLCV chunk for {symbol}: {e}")
                time.sleep(1)
                break

        if not all_ohlcv:
            logger.warning(f"No OHLCV data fetched for {symbol} ({timeframe}).")
            return pd.DataFrame()

        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]

        if len(df) > limit:
            df = df.iloc[-limit:]

        return df


    def fetch_historical_ohlcv(self, symbol, timeframe, start_date_str, end_date_str):
        if not self.markets: return pd.DataFrame() 
        if not self.exchange.has['fetchOHLCV']:
            raise ccxt.NotSupported("fetchOHLCV not supported by the exchange.")

        start_ts = int(self.exchange.parse8601(start_date_str + 'T00:00:00Z'))
        end_ts = int(self.exchange.parse8601(end_date_str + 'T23:59:59Z'))
        timeframe_duration_in_ms = self.exchange.parse_timeframe(timeframe) * 1000
        all_ohlcv = []
        fetch_limit = 200
        current_ts = start_ts
        logger.info(f"Starte historischen Download für {symbol} ({timeframe}) von {start_date_str} bis {end_date_str}")

        while current_ts < end_ts:
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, current_ts, fetch_limit)
                if not ohlcv: break
                ohlcv = [candle for candle in ohlcv if candle[0] <= end_ts]
                if not ohlcv: break
                all_ohlcv.extend(ohlcv)
                current_ts = ohlcv[-1][0] + timeframe_duration_in_ms
                last_date = pd.to_datetime(ohlcv[-1][0], unit='ms', utc=True).strftime('%Y-%m-%d')
                logger.info(f"Daten bis {last_date} heruntergeladen...")
                time.sleep(self.exchange.rateLimit / 1000)
            except ccxt.RateLimitExceeded as e:
                logger.warning(f"Rate limit exceeded during historical fetch: {e}. Waiting...")
                time.sleep(10)
            except Exception as e:
                logger.error(f"Error fetching historical OHLCV chunk: {e}")
                time.sleep(5)

        if not all_ohlcv:
            logger.warning(f"Keine historischen OHLCV-Daten für {symbol} im Zeitraum gefunden.")
            return pd.DataFrame()

        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='last')]
        df = df[(df.index >= pd.to_datetime(start_date_str, utc=True)) & (df.index <= pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True))]

        logger.info(f"Historischer Download abgeschlossen. {len(df)} Kerzen geladen.")
        return df

    # --- Getter Methoden ---
    def fetch_ticker(self, symbol):
        if not self.markets: return None 
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Tickers für {symbol}: {e}")
            raise e

    def fetch_min_amount_tradable(self, symbol: str) -> float:
        if not self.markets: return 0.0 
        try:
            if symbol not in self.markets:
                logger.warning(f"Markt {symbol} nicht gefunden. Lade erneut...")
                self.markets = self.exchange.load_markets()
                if symbol not in self.markets:
                    logger.error(f"Markt {symbol} konnte auch nach erneutem Laden nicht gefunden werden.")
                    return 0.0

            min_amount = self.markets[symbol].get('limits', {}).get('amount', {}).get('min')
            if min_amount is None:
                logger.warning(f"Minimale Handelsmenge für {symbol} nicht gefunden. Verwende Fallback 0.0")
                return 0.0
            return float(min_amount)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der minimalen Handelsmenge für {symbol}: {e}")
            return 0.0


    def amount_to_precision(self, symbol: str, amount: float) -> str:
        if not self.markets: return str(amount) 
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception as e:
            logger.error(f"Fehler beim Konvertieren der Menge {amount} für {symbol}: {e}")
            return str(amount)


    def price_to_precision(self, symbol: str, price: float) -> str:
        if not self.markets: return str(price) 
        try:
            return self.exchange.price_to_precision(symbol, price)
        except Exception as e:
            logger.error(f"Fehler beim Konvertieren des Preises {price} für {symbol}: {e}")
            return str(price)

    def fetch_balance_usdt(self):
        if not self.markets: return 0.0
        try:
            params = {'marginCoin': 'USDT', 'productType': 'USDT-FUTURES'} # productType hinzugefügt
            balance = self.exchange.fetch_balance(params=params)
            usdt_balance = 0.0

            # Verschiedene Strukturen prüfen
            if 'USDT' in balance and 'free' in balance['USDT'] and balance['USDT']['free'] is not None:
                 usdt_balance = float(balance['USDT']['free'])
            elif 'info' in balance and isinstance(balance['info'], list):
                for asset_info in balance['info']:
                    if asset_info.get('marginCoin') == 'USDT':
                        usdt_balance = float(asset_info.get('available', 0.0))
                        break
            elif 'info' in balance and isinstance(balance['info'], dict) and 'USDT' in balance['info']:
                 usdt_balance = float(balance['info']['USDT'].get('available', 0.0))
            
            # Fallback auf 'equity' wenn 'available' fehlt (kann bei Unified Accounts vorkommen)
            if usdt_balance == 0.0 and 'info' in balance and isinstance(balance['info'], list):
                 for asset_info in balance['info']:
                    if asset_info.get('marginCoin') == 'USDT' and 'equity' in asset_info:
                        usdt_balance = float(asset_info.get('equity', 0.0))
                        logger.debug("Verwende 'equity' als Fallback für Kontostand.")
                        break

            # Fallback auf total, wenn free/available/equity nicht gefunden
            if usdt_balance == 0.0 and 'total' in balance and 'USDT' in balance['total']:
                usdt_balance = float(balance['total']['USDT'])
                logger.debug("Verwende 'total' als Fallback für Kontostand.")

            logger.info(f"Verfügbares USDT-Guthaben: {usdt_balance:.2f}")
            return usdt_balance

        except ccxt.AuthenticationError as e:
            logger.critical(f"Authentifizierungsfehler beim Abrufen des Kontostands: {e}. API-Keys prüfen!")
            return 0.0
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Kontostands: {e}", exc_info=True)
            return 0.0

    # --- Order & Positions Management Methoden ---

    def fetch_order(self, id: str, symbol: str):
        if not self.markets: return None
        try:
            params = {'productType': 'USDT-FUTURES'}
            return self.exchange.fetch_order(id, symbol, params=params)
        except ccxt.OrderNotFound:
            logger.warning(f"Order {id} für {symbol} nicht gefunden.")
            return None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen von Order {id} für {symbol}: {e}")
            raise e

    def fetch_open_orders(self, symbol: str):
        if not self.markets: return []
        try:
            params = {'stop': False, 'productType': 'USDT-FUTURES'}
            return self.exchange.fetch_open_orders(symbol, params=params)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen offener Orders für {symbol}: {e}")
            return []

    def fetch_open_trigger_orders(self, symbol: str):
        if not self.markets: return []
        try:
            params = {'stop': True, 'productType': 'USDT-FUTURES'}
            return self.exchange.fetch_open_orders(symbol, params=params)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen offener Trigger-Orders für {symbol}: {e}")
            return []

    def fetch_closed_trigger_orders(self, symbol: str, limit: int = 20):
        if not self.markets: return []
        try:
            closed_triggers = []
            params = {'stop': True, 'productType': 'USDT-FUTURES'} 
            if self.exchange.has['fetchOrders']:
                all_orders = self.exchange.fetchOrders(symbol, limit=limit*3, params=params)
                closed_triggers = [o for o in all_orders if o.get('stopPrice') is not None and o.get('status') in ['closed', 'canceled']]
            else:
                 logger.warning("fetchOrders wird nicht unterstützt, um geschlossene Trigger zu prüfen.")
                 return []

            closed_triggers.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            return closed_triggers[:limit]
        except Exception as e:
            logger.error(f"Fehler beim Abrufen geschlossener Trigger-Orders für {symbol}: {e}")
            return []


    def cancel_order(self, id: str, symbol: str):
        if not self.markets: return None
        try:
            params = {'stop': False, 'productType': 'USDT-FUTURES'}
            return self.exchange.cancel_order(id, symbol, params=params)
        except ccxt.OrderNotFound:
            logger.debug(f"Normale Order {id} für {symbol} beim Stornieren nicht gefunden.")
            return None
        except Exception as e:
            logger.error(f"Fehler beim Stornieren der normalen Order {id} für {symbol}: {e}")
            raise e


    def cancel_trigger_order(self, id: str, symbol: str):
        if not self.markets: return None
        try:
            params = {'stop': True, 'productType': 'USDT-FUTURES'}
            return self.exchange.cancel_order(id, symbol, params=params)
        except ccxt.OrderNotFound:
            logger.debug(f"Trigger Order {id} für {symbol} beim Stornieren nicht gefunden.")
            return None
        except Exception as e:
            logger.error(f"Fehler beim Stornieren der Trigger Order {id} für {symbol}: {e}")
            raise e
            
    def cancel_all_orders_for_symbol(self, symbol):
        """Storniert alle offenen Orders (normal und trigger) für ein Symbol."""
        if not self.markets: return 0
        cancelled_count = 0

        # 1. Normale Orders stornieren (stop: False)
        try:
            logger.info(f"Sende Befehl 'cancelAllOrders' (Normal) für {symbol}...")
            self.exchange.cancel_all_orders(symbol, params={'productType': 'USDT-FUTURES', 'stop': False})
            cancelled_count += 1
            time.sleep(0.5) 
        except ccxt.ExchangeError as e:
            if 'Order not found' in str(e) or 'no order to cancel' in str(e).lower() or '22001' in str(e):
                logger.info("Keine normalen Orders zum Stornieren gefunden.")
            else:
                logger.error(f"Fehler beim Stornieren normaler Orders: {e}")
        except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Stornieren normaler Orders: {e}")

        # 2. Trigger Orders stornieren (stop: True)
        try:
            logger.info(f"Sende Befehl 'cancelAllOrders' (Trigger/Stop) für {symbol}...")
            self.exchange.cancel_all_orders(symbol, params={'productType': 'USDT-FUTURES', 'stop': True})
            cancelled_count += 1
            time.sleep(0.5)
        except ccxt.ExchangeError as e:
            if 'Order not found' in str(e) or 'no order to cancel' in str(e).lower() or '22001' in str(e):
                logger.info("Keine Trigger-Orders zum Stornieren gefunden.")
            else:
                logger.error(f"Fehler beim Stornieren von Trigger-Orders: {e}")
        except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Stornieren von Trigger-Orders: {e}")

        return cancelled_count


    def fetch_open_positions(self, symbol):
        if not self.markets: return []
        try:
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            positions = self.exchange.fetch_positions([symbol], params=params)
            open_positions = []
            for p in positions:
                 try:
                     size_key = 'contracts' if 'contracts' in p else 'contractSize'
                     contracts_str = p.get(size_key)
                     if contracts_str is not None and abs(float(contracts_str)) > 1e-9:
                          open_positions.append(p)
                     elif p.get('initialMargin', 0) > 0 or p.get('maintMargin', 0) > 0:
                         if contracts_str is None or abs(float(contracts_str)) <= 1e-9 :
                              logger.warning(f"Position für {symbol} hat Margin > 0 aber Size ≈ 0. Betrachte sie als offen. Details: {p}")
                         open_positions.append(p)
                 except (ValueError, TypeError, KeyError) as e:
                     logger.warning(f"Konnte Positionsgröße nicht prüfen: {e}. Positionsdaten: {p}")
                     continue
            return open_positions
        except Exception as e:
            logger.error(f"Fehler beim Abrufen offener Positionen für {symbol}: {e}", exc_info=True)
            return []


    def close_position(self, symbol: str, side: Optional[str] = None):
        if not self.markets: return None
        try:
            position_list = self.fetch_open_positions(symbol)
            if not position_list:
                logger.warning(f"Keine offene Position zum Schließen für {symbol} gefunden.")
                return None
            position = position_list[0]
            close_side = 'sell' if position['side'] == 'long' else 'buy'
            size_key = 'contracts' if 'contracts' in position else 'contractSize'
            amount = position.get(size_key)
            if amount is None:
                 logger.error(f"Konnte Positionsgröße ('{size_key}') nicht aus Positionsdaten lesen: {position}")
                 return None
            logger.info(f"Schließe {position['side']} Position für {symbol} mit Market Order (Menge: {amount}).")
            return self.place_market_order(symbol, close_side, float(amount), reduce=True)
        except Exception as e:
            logger.error(f"Fehler beim Schließen der Position für {symbol}: {e}")
            raise e


    def set_margin_mode(self, symbol, margin_mode='isolated'):
        if not self.markets: return
        margin_mode_lower = margin_mode.lower()
        if margin_mode_lower not in ['isolated', 'cross']:
            logger.error(f"Ungültiger Margin-Modus: {margin_mode}.")
            return
        try:
            params={'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            self.exchange.set_margin_mode(margin_mode_lower, symbol, params=params)
            logger.info(f"Margin-Modus für {symbol} auf '{margin_mode_lower}' gesetzt.")
        except ccxt.ExchangeError as e:
            if 'Margin mode is the same' in str(e) or 'margin mode is not changed' in str(e).lower() or '40051' in str(e):
                logger.debug(f"Margin-Modus für {symbol} ist bereits '{margin_mode_lower}'.")
            else:
                logger.error(f"Fehler beim Setzen des Margin-Modus für {symbol}: {e}")
        except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Setzen des Margin-Modus für {symbol}: {e}")

    def set_leverage(self, symbol, leverage, margin_mode='isolated'):
        if not self.markets: return
        try:
            leverage = int(leverage)
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}

            if margin_mode.lower() == 'isolated':
                params_long = {**params, 'holdSide': 'long'}
                self.exchange.set_leverage(leverage, symbol, params=params_long)
                logger.debug(f"Isolated Leverage für {symbol} (Long) auf {leverage}x gesetzt.")
                time.sleep(0.2)
                params_short = {**params, 'holdSide': 'short'}
                self.exchange.set_leverage(leverage, symbol, params=params_short)
                logger.debug(f"Isolated Leverage für {symbol} (Short) auf {leverage}x gesetzt.")
            else: 
                self.exchange.set_leverage(leverage, symbol, params=params)
                logger.debug(f"Cross Leverage für {symbol} auf {leverage}x gesetzt.")
            logger.info(f"Hebel für {symbol} ({margin_mode}) auf {leverage}x gesetzt.")
        except ccxt.ExchangeError as e:
            if 'Leverage not changed' in str(e) or 'leverage is not modified' in str(e).lower() or '40052' in str(e):
                logger.debug(f"Hebel für {symbol} ist bereits {leverage}x.")
            else:
                logger.error(f"Fehler beim Setzen des Hebels für {symbol}: {e}")
        except Exception as e:
            logger.error(f"Unerwarteter Fehler beim Setzen des Hebels für {symbol}: {e}")

    # --- Order Platzierungs Methoden ---

    def place_market_order(self, symbol: str, side: str, amount: float, reduce: bool = False, params={}):
        if not self.markets: return None
        try:
            order_params = {'reduceOnly': reduce, **params} 
            if 'productType' not in order_params: # Sicherstellen, dass productType gesetzt ist
                order_params['productType'] = 'USDT-FUTURES'
            amount_str = self.amount_to_precision(symbol, amount)
            logger.info(f"Platziere Market Order: {side.upper()} {amount_str} {symbol} (Params: {order_params})")
            return self.exchange.create_order(symbol, 'market', side, float(amount_str), params=order_params)
        except ccxt.InsufficientFunds as e:
            logger.error(f"Nicht genügend Guthaben für Market Order {side} {amount} {symbol}: {e}")
            raise e
        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Market Order für {symbol}: {e}", exc_info=True)
            raise e

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float, reduce: bool = False, params={}):
        if not self.markets: return None
        try:
            order_params = {'reduceOnly': reduce, **params}
            if 'productType' not in order_params:
                order_params['productType'] = 'USDT-FUTURES'
            amount_str = self.amount_to_precision(symbol, amount)
            price_str = self.price_to_precision(symbol, price)
            logger.info(f"Platziere Limit Order: {side.upper()} {amount_str} {symbol} @ {price_str} (Params: {order_params})")
            return self.exchange.create_order(symbol, 'limit', side, float(amount_str), float(price_str), params=order_params)
        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Limit Order für {symbol}: {e}", exc_info=True)
            raise e

    # =========================================================================
    # HIER IST DIE KORREKTUR FÜR TRIGGER MARKET ORDERS (TP/SL)
    # WIR KOPIEREN DIE LOGIK VON JAEGERBOT/TITANBOT
    # =========================================================================
    def place_trigger_market_order(self, symbol: str, side: str, amount: float, trigger_price: float, reduce: bool = False, params={}):
        """Platziert eine Trigger-Market Order (Stop Market oder Take Profit Market)."""
        if not self.markets: return None
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            trigger_price_str = self.price_to_precision(symbol, trigger_price)

            # *** KORREKTUR: Vereinfachte Params, basierend auf JaegerBot/TitanBot ***
            # Wir übergeben NUR 'triggerPrice' und 'reduceOnly'. 
            # ccxt wandelt 'triggerPrice' intern in 'stopPrice' um.
            # KEIN 'planType' oder 'triggerType' hier!
            order_params = {
                'triggerPrice': trigger_price_str,
                'reduceOnly': reduce,
                **params # Fügt zusätzliche Params hinzu, z.B. productType falls nötig
            }
            
            # Stelle sicher, dass productType für Bitget vorhanden ist
            if 'productType' not in order_params:
                 order_params['productType'] = 'USDT-FUTURES'

            logger.info(f"Platziere Trigger Market Order: {side.upper()} {amount_str} {symbol}, Params: {order_params}")
            order = self.exchange.create_order(symbol, 'market', side, float(amount_str), params=order_params)
            return order

        except ccxt.ExchangeError as e:
            logger.error(f"Börsenfehler beim Platzieren der Trigger Market Order für {symbol}: {e}", exc_info=True)
            raise e
        except Exception as e:
            logger.error(f"Allgemeiner Fehler beim Platzieren der Trigger Market Order für {symbol}: {e}", exc_info=True)
            raise e

    # =========================================================================
    # HIER IST DIE KORREKTUR FÜR TRIGGER LIMIT ORDERS (ENTRY)
    # =========================================================================
    def place_trigger_limit_order(self, symbol: str, side: str, amount: float, trigger_price: float, price: float, reduce: bool = False, params={}):
        """Platziert eine Trigger-Limit Order."""
        if not self.markets: return None
        try:
            amount_str = self.amount_to_precision(symbol, amount)
            trigger_price_str = self.price_to_precision(symbol, trigger_price)
            price_str = self.price_to_precision(symbol, price) # Limit Preis nach Trigger

            # *** KORREKTUR: Dieselbe vereinfachte Logik ***
            # Wir übergeben NUR 'triggerPrice' und 'reduceOnly'.
            # Der Unterschied ist, dass der 'type' in create_order 'limit' ist.
            order_params = {
                'triggerPrice': trigger_price_str,
                'reduceOnly': reduce,
                **params
            }
            
            if 'productType' not in order_params:
                 order_params['productType'] = 'USDT-FUTURES'

            logger.info(f"Platziere Trigger Limit Order: {side.upper()} {amount_str} {symbol}, Params: {order_params}, Limit: {price_str}")
            order = self.exchange.create_order(symbol, 'limit', side, float(amount_str), float(price_str), params=order_params)
            return order
        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Trigger Limit Order für {symbol}: {e}", exc_info=True)
            raise e

    # --- Implizite Methoden (Trailing Stop) ---
    # Diese Funktion wird vom ltbtbot nicht verwendet, aber wir lassen sie drin,
    # da sie im Original-Code war (und von Jaeger/Titan kopiert wurde).
    def place_trailing_stop_order(self, symbol, side, amount, activation_price, callback_rate_decimal, params={}):
        if not self.markets: return None
        try:
            market_id = self.exchange.market(symbol)['id']
            margin_coin = 'USDT'

            if side.lower() == 'sell': api_side = 'close_long'
            elif side.lower() == 'buy': api_side = 'close_short'
            else: raise ValueError(f"Ungültiger 'side' für Trailing Stop: {side}")

            api_callback_rate_str = str(callback_rate_decimal * 100.0)

            # Diese Params SIND korrekt für TSL (planType='trailing_stop')
            api_params = {
                'symbol': market_id,
                'marginCoin': margin_coin,
                'planType': 'trailing_stop', 
                'side': api_side,
                'size': str(self.amount_to_precision(symbol, amount)),
                'triggerPrice': str(self.price_to_precision(symbol, activation_price)), 
                'rangeRate': api_callback_rate_str, 
                **params
            }

            logger.info(f"Sende impliziten TSL-Aufruf: private_mix_post_plan_place_plan mit Params: {api_params}")
            response = self.exchange.private_mix_post_plan_place_plan(api_params)
            logger.info(f"TSL-Antwort von Bitget API: {response}")
            return response
        except AttributeError as e:
            logger.error(f"Implizite Methode für Trailing Stop nicht gefunden: {e}")
            raise ccxt.NotSupported("Trailing Stop via impliziter Methode nicht verfügbar.")
        except Exception as e:
            logger.error(f"Kritischer Fehler beim Aufruf der impliziten TSL-Methode: {e}", exc_info=True)
            raise e
