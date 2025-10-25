# src/ltbbot/analysis/backtester.py
import os
import pandas as pd
import numpy as np
from datetime import timedelta
import json
import sys
import logging
# tqdm import entfernt für sauberere Optuna-Ausgabe
# from tqdm import tqdm

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
            # Info-Log nur ausgeben, wenn relevant (z.B. Loglevel DEBUG)
            # logger.info(f"Lade Daten für {symbol} ({timeframe}) aus Cache: {cache_file}")
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
                 # logger.info("Cache deckt den Zeitraum ab. Filtere Daten.") # Zu laut für Optuna
                 return data.loc[req_start:req_end].copy() # Kopie zurückgeben
            else:
                 logger.info(f"Cache für {symbol} ({timeframe}) deckt Zeitraum NICHT ab. Download notwendig.")
                 data = pd.DataFrame() # Leere Daten, um Download zu erzwingen
        except Exception as e:
            logger.error(f"Fehler beim Lesen oder Verarbeiten der Cache-Datei {cache_file}: {e}")
            data = pd.DataFrame() # Leere Daten bei Fehler

    # --- Versuch 2: Von Börse herunterladen ---
    if data.empty:
        logger.info(f"Starte Download für {symbol} ({timeframe}) von der Börse [{start_date_str} bis {end_date_str}]...")
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

    # Sollte nur erreicht werden, wenn Cache existiert, aber Zeitraum nicht passt UND Download fehlschlägt
    logger.error(f"Konnte Daten für {symbol} ({timeframe}) weder aus Cache laden noch herunterladen.")
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

    # Setze Logger-Level für diesen Lauf (wird von Optimizer gesteuert)
    # logger.debug(f"Starte Envelope Backtest mit Params: {params}")

    # --- Parameter extrahieren ---
    strategy_params = params['strategy']
    risk_params = params['risk']
    behavior_params = params['behavior']

    leverage = risk_params['leverage']
    balance_fraction = risk_params['balance_fraction_pct'] / 100.0
    num_envelopes = len(strategy_params['envelopes'])
    stop_loss_pct = risk_params['stop_loss_pct'] / 100.0 # Als Dezimalzahl
    use_longs = behavior_params.get('use_longs', True)
    use_shorts = behavior_params.get('use_shorts', True)

    fee_pct = 0.0006 # Beispiel: 0.06% Maker/Taker Fee (anpassen!)

    # --- Indikatoren berechnen ---
    try:
         df, _ = calculate_indicators_and_signals(data.copy(), params)
         if df.empty:
             raise ValueError("Indikatorberechnung ergab leeres DataFrame.")
    except Exception as e:
        logger.warning(f"Fehler bei Indikatorberechnung im Backtest: {e}")
        # Gib einen negativen Score zurück, damit Optuna diesen Trial verwirft
        return {"total_pnl_pct": -1000, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0}


    # --- Initialisierung für den Backtest-Loop ---
    capital = start_capital
    peak_capital = start_capital
    max_drawdown_pct = 0.0

    positions = [] # Liste für aktive Positions-Layer [{entry_price, amount_coins, side, sl_price, tp_price}, ...]
                   # 'amount_coins' ist die Menge *ohne* Hebel. PnL wird mit Hebel berechnet.
    closed_trades = [] # Liste für abgeschlossene Trades [{pnl, side}, ...]

    # logger.info("Starte Backtest-Loop...") # Zu laut für Optuna

    for i in range(len(df)): # Gehe durch jede Kerze des **indikatorberechneten** DataFrames
        current_candle = df.iloc[i]

        # Kapital zu Beginn der Kerze für DD-Berechnung speichern
        capital_snapshot_start_of_candle = capital

        # --- Ausstiege prüfen (TP und SL) ---
        remaining_positions = []
        exit_pnl_current_candle = 0.0

        for pos in positions:
            exited = False
            exit_price = None
            pnl = 0.0

            # SL Prüfung
            if pos['side'] == 'long' and current_candle['low'] <= pos['sl_price']:
                exit_price = pos['sl_price']
                exited = True
            elif pos['side'] == 'short' and current_candle['high'] >= pos['sl_price']:
                exit_price = pos['sl_price']
                exited = True

            # TP Prüfung (nur wenn SL nicht getroffen)
            if not exited:
                tp_price_current = current_candle['average'] # TP ist der Average *dieser* Kerze
                if pos['side'] == 'long' and current_candle['high'] >= tp_price_current:
                    if current_candle['open'] >= tp_price_current or current_candle['low'] <= tp_price_current:
                         exit_price = tp_price_current
                         exited = True
                elif pos['side'] == 'short' and current_candle['low'] <= tp_price_current:
                     if current_candle['open'] <= tp_price_current or current_candle['high'] >= tp_price_current:
                         exit_price = tp_price_current
                         exited = True

            # Wenn Ausstieg, PnL berechnen und Position entfernen
            if exited and exit_price is not None:
                # *** KORREKTUR DER PNL-FORMEL: leverage NICHT ERNEUT ANWENDEN ***
                # amount_coins ist die Menge, die dem Margin entspricht
                # Der PnL wird durch die Preisdifferenz * gehebelte Menge erzielt
                # Gehebelte Menge = amount_coins * leverage
                # Oder einfacher: PnL = Preisdiff * amount_coins * leverage
                # Wobei amount_coins = margin / entry_price war

                if pos['side'] == 'long':
                    # PnL = (Verkauf - Kauf) * Menge_Coins * Hebel
                    pnl = (exit_price - pos['entry_price']) * pos['amount_coins'] * leverage
                else: # short
                    # PnL = (Verkauf - Kauf) * Menge_Coins * Hebel
                    # Verkauf = entry_price, Kauf = exit_price
                    pnl = (pos['entry_price'] - exit_price) * pos['amount_coins'] * leverage

                # Gebühren abziehen (Berechnung basiert auf gehebeltem Wert)
                # Notional Value = entry_price * amount_coins * leverage
                entry_notional_value = pos['entry_price'] * pos['amount_coins'] * leverage
                exit_notional_value = exit_price * pos['amount_coins'] * leverage
                fees = (entry_notional_value * fee_pct) + (exit_notional_value * fee_pct)
                pnl -= fees

                exit_pnl_current_candle += pnl
                closed_trades.append({'pnl': pnl, 'side': pos['side']})
            else:
                remaining_positions.append(pos) # Position bleibt offen

        positions = remaining_positions
        # Realisiertes Kapital nach Ausstiegen aktualisieren
        capital += exit_pnl_current_candle

        # --- Einstiege prüfen ---
        # Berechne verfügbares Kapital pro neuer Order basierend auf aktuellem *realisierten* Kapital
        balance_for_trades = capital * balance_fraction
        margin_per_order = balance_for_trades / num_envelopes if num_envelopes > 0 else 0

        # Long Entries
        if use_longs and margin_per_order > 0 and capital > 0: # Nur wenn Kapital und Margin verfügbar
            for k in range(1, num_envelopes + 1):
                low_band_col = f'band_low_{k}'
                if low_band_col not in current_candle: continue
                entry_trigger_price = current_candle[low_band_col]

                if current_candle['low'] <= entry_trigger_price:
                    entry_price = entry_trigger_price # Vereinfacht: Einstieg zum Triggerpreis
                    if entry_price > 0:
                        # Menge_Coins = Margin / Entry_Preis
                        amount_coins = margin_per_order / entry_price
                        sl_price = entry_price * (1 - stop_loss_pct)
                        tp_price = current_candle['average'] # TP ist der aktuelle Durchschnitt

                        # Prüfen, ob *genügend Margin* für diesen Trade vorhanden ist
                        # (Theoretisch schon durch capital_per_order abgedeckt, aber als Sicherheitscheck)
                        # Hier vereinfacht: Wir gehen davon aus, dass margin_per_order <= capital ist.
                        # In einer komplexeren Simulation müsste man den gesamten verwendeten Margin tracken.

                        positions.append({
                            'entry_price': entry_price,
                            'amount_coins': amount_coins, # Menge OHNE Hebel speichern
                            'side': 'long',
                            'sl_price': sl_price,
                            'tp_price': tp_price
                        })
                        # logger.debug(f"Long Entry @ {entry_price:.4f}...") # Zu laut

        # Short Entries
        if use_shorts and margin_per_order > 0 and capital > 0:
             for k in range(1, num_envelopes + 1):
                 high_band_col = f'band_high_{k}'
                 if high_band_col not in current_candle: continue
                 entry_trigger_price = current_candle[high_band_col]

                 if current_candle['high'] >= entry_trigger_price:
                     entry_price = entry_trigger_price
                     if entry_price > 0:
                         amount_coins = margin_per_order / entry_price
                         sl_price = entry_price * (1 + stop_loss_pct)
                         tp_price = current_candle['average']

                         positions.append({
                            'entry_price': entry_price,
                            'amount_coins': amount_coins, # Menge OHNE Hebel speichern
                            'side': 'short',
                            'sl_price': sl_price,
                            'tp_price': tp_price
                        })
                         # logger.debug(f"Short Entry @ {entry_price:.4f}...") # Zu laut

        # --- Drawdown berechnen (basierend auf Kapital zu Beginn der Kerze) ---
        if capital_snapshot_start_of_candle > 0: # Verhindere Division durch Null
             # Drawdown wird vom Peak berechnet
             current_drawdown = (peak_capital - capital_snapshot_start_of_candle) / peak_capital if peak_capital > 0 else 0
             max_drawdown_pct = max(max_drawdown_pct, current_drawdown * 100)

        # Peak nach allen Aktionen in der Kerze aktualisieren
        peak_capital = max(peak_capital, capital) # Peak basiert auf realisiertem Kapital

        # --- Abbruch bei Totalverlust (basierend auf realisiertem Kapital) ---
        if capital <= 0:
            logger.warning(f"Reales Kapital <= 0 ({capital:.2f}). Backtest abgebrochen bei Kerze {i}.")
            capital = 0 # Sicherstellen, dass es nicht negativ ist für Endergebnis
            break # Loop beenden

    # --- Endauswertung ---
    # Berechne finalen Equity inkl. unrealisiertem PnL am Ende (optional für Report, nicht für Optimierung)
    final_unrealized_pnl = 0.0
    if not df.empty:
         last_close_price = df.iloc[-1]['close']
         for pos in positions:
              if pos['side'] == 'long':
                  final_unrealized_pnl += (last_close_price - pos['entry_price']) * pos['amount_coins'] * leverage
              else: # short
                  final_unrealized_pnl += (pos['entry_price'] - last_close_price) * pos['amount_coins'] * leverage
    # Finaler Equity = Realisiertes Kapital am Ende + Unrealisierter PnL der offenen Positionen
    final_equity = capital + final_unrealized_pnl
    # Begrenze final_equity auf maximal 0, falls negativ
    final_equity = max(0, final_equity)

    total_pnl = final_equity - start_capital
    total_pnl_pct = (total_pnl / start_capital) * 100 if start_capital != 0 else 0
    trades_count = len(closed_trades)
    wins_count = sum(1 for trade in closed_trades if trade['pnl'] > 0)
    win_rate = (wins_count / trades_count) * 100 if trades_count > 0 else 0

    # logger.info("Backtest-Loop beendet.") # Zu laut für Optuna

    results = {
        "total_pnl_pct": round(total_pnl_pct, 2),
        "trades_count": trades_count,
        "win_rate": round(win_rate, 2),
        # Max Drawdown wird als positiver %-Wert zurückgegeben
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "end_capital": round(final_equity, 2), # Verwende finalen Equity inkl. unreal. PnL
        "start_capital": start_capital
    }
    # logger.debug(f"Backtest Ergebnisse: {results}") # Zu laut für Optuna
    return results
