# src/ltbbot/analysis/backtester.py
import os
import pandas as pd
import numpy as np
from datetime import timedelta
import json
import sys
import logging
from tqdm import tqdm # Für Fortschrittsanzeige

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange # Für load_data
# Importiere die Indikatorberechnung
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

def load_data(symbol, timeframe, start_date_str, end_date_str):
    """Lädt historische OHLCV-Daten, entweder aus dem Cache oder von der Börse."""
    cache_dir = os.path.join(PROJECT_ROOT, 'data', 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    symbol_filename = symbol.replace('/', '-').replace(':', '-')
    cache_file = os.path.join(cache_dir, f"{symbol_filename}_{timeframe}.csv")

    data = pd.DataFrame() # Initialisiere leeres DataFrame

    # --- Versuch 1: Aus Cache laden ---
    if os.path.exists(cache_file):
        try:
            logger.info(f"Lade Daten für {symbol} ({timeframe}) aus Cache: {cache_file}")
            data = pd.read_csv(cache_file, index_col='timestamp', parse_dates=True)
            # Konvertiere Index sicher zu UTC, falls nicht bereits geschehen
            if data.index.tz is None:
                 data.index = data.index.tz_localize('UTC')
            else:
                 data.index = data.index.tz_convert('UTC')

            # Prüfe, ob der Cache den angeforderten Zeitraum abdeckt
            cache_start = data.index.min()
            cache_end = data.index.max()
            req_start = pd.to_datetime(start_date_str, utc=True)
            req_end = pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True) # Ende des Tages einschließen

            if cache_start <= req_start and cache_end >= req_end:
                 logger.info("Cache deckt den Zeitraum ab. Filtere Daten.")
                 return data.loc[req_start:req_end].copy() # Kopie zurückgeben
            else:
                 logger.info("Cache deckt den Zeitraum NICHT vollständig ab. Download notwendig.")
                 data = pd.DataFrame() # Leere Daten, um Download zu erzwingen
        except Exception as e:
            logger.error(f"Fehler beim Lesen oder Verarbeiten der Cache-Datei {cache_file}: {e}")
            data = pd.DataFrame() # Leere Daten bei Fehler

    # --- Versuch 2: Von Börse herunterladen ---
    if data.empty:
        logger.info(f"Starte Download für {symbol} ({timeframe}) von der Börse...")
        try:
            # Lade API-Keys nur für den Download
            secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
            with open(secret_path, "r") as f:
                secrets = json.load(f)
            # Verwende den ersten Account aus der Liste
            api_setup = secrets.get('ltbbot')[0]
            exchange = Exchange(api_setup) # Eigene Exchange-Instanz für Download

            # fetch_historical_ohlcv sollte ein DataFrame mit UTC-Timestamp-Index zurückgeben
            full_data = exchange.fetch_historical_ohlcv(symbol, timeframe, start_date_str, end_date_str)

            if full_data is not None and not full_data.empty:
                logger.info(f"Download erfolgreich. Speichere {len(full_data)} Kerzen im Cache: {cache_file}")
                # Stelle sicher, dass der Index UTC ist, bevor gespeichert wird
                if full_data.index.tz is None:
                     full_data.index = full_data.index.tz_localize('UTC')
                else:
                     full_data.index = full_data.index.tz_convert('UTC')
                full_data.to_csv(cache_file)
                # Filtere erneut auf den exakten Zeitraum (falls fetch mehr geliefert hat)
                req_start = pd.to_datetime(start_date_str, utc=True)
                req_end = pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True)
                return full_data.loc[req_start:req_end].copy() # Kopie zurückgeben
            else:
                logger.error(f"Download für {symbol} ({timeframe}) fehlgeschlagen oder keine Daten erhalten.")
                return pd.DataFrame()
        except FileNotFoundError:
             logger.error(f"secret.json nicht gefunden unter {secret_path}. Download nicht möglich.")
             return pd.DataFrame()
        except IndexError:
             logger.error(f"Kein Account in secret.json unter dem Key 'ltbbot' gefunden.")
             return pd.DataFrame()
        except Exception as e:
            logger.error(f"Fehler beim Daten-Download für {symbol} ({timeframe}): {e}", exc_info=True)
            return pd.DataFrame()

    return pd.DataFrame() # Fallback

# --- NEUER BACKTESTER FÜR ENVELOPE ---

def run_envelope_backtest(data, params, start_capital=1000):
    """
    Führt einen Backtest für die Envelope-Strategie durch.

    Args:
        data (pd.DataFrame): OHLCV-Daten mit Timestamp-Index (UTC).
        params (dict): Strategie- und Risikoparameter.
        start_capital (float): Startkapital.

    Returns:
        dict: Ergebnisse des Backtests.
    """
    if data.empty:
        logger.warning("Leeres DataFrame an Backtester übergeben.")
        return {"total_pnl_pct": 0, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": start_capital}

    logger.debug(f"Starte Envelope Backtest mit Params: {params}")

    # --- Parameter extrahieren ---
    strategy_params = params['strategy']
    risk_params = params['risk']
    behavior_params = params['behavior']

    leverage = risk_params['leverage']
    balance_fraction = risk_params['balance_fraction_pct'] / 100.0
    num_envelopes = len(strategy_params['envelopes'])
    stop_loss_pct = risk_params['stop_loss_pct'] / 100.0
    use_longs = behavior_params.get('use_longs', True)
    use_shorts = behavior_params.get('use_shorts', True)

    fee_pct = 0.0006 # Beispiel: 0.06% Maker/Taker Fee (anpassen!)

    # --- Indikatoren berechnen ---
    try:
         # Wichtig: calculate_indicators_and_signals gibt jetzt auch die Bandpreise zurück
         # Wir brauchen nur das DataFrame mit den Indikatoren für den Loop
         df, _ = calculate_indicators_and_signals(data.copy(), params)
         if df.empty:
             raise ValueError("Indikatorberechnung ergab leeres DataFrame.")
    except Exception as e:
        logger.error(f"Fehler bei der Indikatorberechnung im Backtester: {e}")
        return {"total_pnl_pct": -100, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0}


    # --- Initialisierung für den Backtest-Loop ---
    capital = start_capital
    peak_capital = start_capital
    max_drawdown_pct = 0.0

    positions = [] # Liste für aktive Positions-Layer [{entry_price, amount, side, sl_price, tp_price}, ...]
    closed_trades = [] # Liste für abgeschlossene Trades [{pnl, side}, ...]

    logger.info("Starte Backtest-Loop...")
    # tqdm für Fortschrittsanzeige
    for i in tqdm(range(len(df)), desc="Backtesting Envelope", leave=False):
        current_candle = df.iloc[i]
        next_open = df['open'].iloc[i+1] if i + 1 < len(df) else current_candle['close'] # Nächster Preis für Ausführung

        current_capital_snapshot = capital # Kapital zu Beginn der Kerze für DD-Berechnung

        # --- Ausstiege prüfen (TP und SL) ---
        remaining_positions = []
        exit_pnl_current_candle = 0.0

        for pos in positions:
            exited = False
            exit_price = None
            pnl = 0.0

            # 1. Stop Loss prüfen
            if pos['side'] == 'long' and current_candle['low'] <= pos['sl_price']:
                exit_price = pos['sl_price'] # SL getroffen
                exited = True
            elif pos['side'] == 'short' and current_candle['high'] >= pos['sl_price']:
                exit_price = pos['sl_price'] # SL getroffen
                exited = True

            # 2. Take Profit prüfen (wenn kein SL getroffen)
            if not exited:
                if pos['side'] == 'long' and current_candle['high'] >= pos['tp_price']:
                    # Prüfen, ob Average zuerst berührt wurde oder Open darüber lag
                    if current_candle['open'] >= pos['tp_price'] or current_candle['low'] <= pos['tp_price']:
                         exit_price = pos['tp_price'] # TP getroffen (Average)
                         exited = True
                elif pos['side'] == 'short' and current_candle['low'] <= pos['tp_price']:
                     if current_candle['open'] <= pos['tp_price'] or current_candle['high'] >= pos['tp_price']:
                         exit_price = pos['tp_price'] # TP getroffen (Average)
                         exited = True

            # Wenn Ausstieg, PnL berechnen und Position entfernen
            if exited and exit_price is not None:
                if pos['side'] == 'long':
                    pnl = (exit_price - pos['entry_price']) * pos['amount'] * leverage
                else: # short
                    pnl = (pos['entry_price'] - exit_price) * pos['amount'] * leverage

                # Gebühren abziehen (Entry + Exit)
                entry_value = pos['entry_price'] * pos['amount'] * leverage
                exit_value = exit_price * pos['amount'] * leverage
                fees = (entry_value * fee_pct) + (exit_value * fee_pct)
                pnl -= fees

                exit_pnl_current_candle += pnl
                closed_trades.append({'pnl': pnl, 'side': pos['side']})
                # Position nicht in remaining_positions aufnehmen
            else:
                remaining_positions.append(pos) # Position bleibt offen

        positions = remaining_positions
        capital += exit_pnl_current_candle # Kapital nach Ausstiegen aktualisieren

        # --- Einstiege prüfen ---
        # Berechne verfügbares Kapital pro neuer Order
        balance_for_trades = capital * balance_fraction
        capital_per_order = balance_for_trades / num_envelopes if num_envelopes > 0 else 0

        # Long Entries
        if use_longs:
            for k in range(1, num_envelopes + 1):
                low_band_col = f'band_low_{k}'
                entry_trigger_price = current_candle[low_band_col] # Einstieg, wenn Tief die Bande berührt/unterschreitet

                if current_candle['low'] <= entry_trigger_price:
                    # Prüfe, ob schon eine Long-Position auf diesem Level existiert (optional, verhindert Doppeleinstieg auf gleicher Kerze/Level)
                    # if any(p['side'] == 'long' and abs(p['entry_price'] - entry_trigger_price) < 1e-6 for p in positions): continue

                    entry_price = min(current_candle['open'], entry_trigger_price) # Einstieg zum Open oder Trigger, je nachdem was niedriger ist
                    if capital_per_order > 0 and entry_price > 0:
                        amount = capital_per_order / entry_price # Menge in Coins (ohne Hebel hier)
                        sl_price = entry_price * (1 - stop_loss_pct)
                        tp_price = current_candle['average'] # TP ist der aktuelle Durchschnitt

                        positions.append({
                            'entry_price': entry_price,
                            'amount': amount,
                            'side': 'long',
                            'sl_price': sl_price,
                            'tp_price': tp_price
                        })
                        # logger.debug(f"Long Entry @ {entry_price:.4f}, Amount: {amount:.4f}, SL: {sl_price:.4f}, TP: {tp_price:.4f}")
                        # Kein Kapitalabzug hier, PnL wird beim Schließen realisiert

        # Short Entries
        if use_shorts:
            for k in range(1, num_envelopes + 1):
                high_band_col = f'band_high_{k}'
                entry_trigger_price = current_candle[high_band_col]

                if current_candle['high'] >= entry_trigger_price:
                    # if any(p['side'] == 'short' and abs(p['entry_price'] - entry_trigger_price) < 1e-6 for p in positions): continue

                    entry_price = max(current_candle['open'], entry_trigger_price)
                    if capital_per_order > 0 and entry_price > 0:
                        amount = capital_per_order / entry_price
                        sl_price = entry_price * (1 + stop_loss_pct)
                        tp_price = current_candle['average']

                        positions.append({
                            'entry_price': entry_price,
                            'amount': amount,
                            'side': 'short',
                            'sl_price': sl_price,
                            'tp_price': tp_price
                        })
                        # logger.debug(f"Short Entry @ {entry_price:.4f}, Amount: {amount:.4f}, SL: {sl_price:.4f}, TP: {tp_price:.4f}")

        # --- Drawdown berechnen (basierend auf Kapital zu Beginn der Kerze) ---
        if capital_snapshot > 0: # Verhindere Division durch Null
             current_drawdown = (peak_capital - capital_snapshot) / peak_capital
             max_drawdown_pct = max(max_drawdown_pct, current_drawdown * 100)
        peak_capital = max(peak_capital, capital) # Peak nach der Kerze aktualisieren

        # --- Abbruch bei Totalverlust ---
        if capital <= 0:
            logger.warning("Kapital auf 0 gefallen. Backtest abgebrochen.")
            capital = 0 # Sicherstellen, dass es nicht negativ ist
            break

    # --- Endauswertung ---
    end_capital = capital
    total_pnl = end_capital - start_capital
    total_pnl_pct = (total_pnl / start_capital) * 100 if start_capital > 0 else 0
    trades_count = len(closed_trades)
    wins_count = sum(1 for trade in closed_trades if trade['pnl'] > 0)
    win_rate = (wins_count / trades_count) * 100 if trades_count > 0 else 0

    logger.info("Backtest-Loop beendet.")

    results = {
        "total_pnl_pct": round(total_pnl_pct, 2),
        "trades_count": trades_count,
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "end_capital": round(end_capital, 2),
        "start_capital": start_capital
    }
    logger.debug(f"Backtest Ergebnisse: {results}")
    return results

# --- Alte ANN Backtest Funktion (kann entfernt oder auskommentiert werden) ---
# def run_ann_backtest(...):
#    ... (alter Code)
