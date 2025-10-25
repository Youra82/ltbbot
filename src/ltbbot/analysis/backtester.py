# src/ltbbot/analysis/backtester.py
import os
import pandas as pd
import numpy as np
from datetime import timedelta
import json
import sys
import logging

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange # Für load_data
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
            data = pd.read_csv(cache_file, index_col='timestamp', parse_dates=True)
            if data.index.tz is None:
                data.index = data.index.tz_localize('UTC')
            else:
                data.index = data.index.tz_convert('UTC')

            cache_start = data.index.min()
            cache_end = data.index.max()
            req_start = pd.to_datetime(start_date_str, utc=True)
            req_end = pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True)

            if cache_start <= req_start and cache_end >= req_end:
                return data.loc[req_start:req_end].copy()
            else:
                logger.info(f"Cache für {symbol} ({timeframe}) deckt Zeitraum NICHT ab. Download notwendig.")
                data = pd.DataFrame()
        except Exception as e:
            logger.error(f"Fehler beim Lesen oder Verarbeiten der Cache-Datei {cache_file}: {e}")
            data = pd.DataFrame()

    # --- Versuch 2: Von Börse herunterladen ---
    if data.empty:
        logger.info(f"Starte Download für {symbol} ({timeframe}) von der Börse [{start_date_str} bis {end_date_str}]...")
        try:
            secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
            with open(secret_path, "r") as f:
                secrets = json.load(f)
            api_setup = secrets.get('ltbbot')[0]
            exchange = Exchange(api_setup)

            full_data = exchange.fetch_historical_ohlcv(symbol, timeframe, start_date_str, end_date_str)

            if full_data is not None and not full_data.empty:
                logger.info(f"Download erfolgreich. Speichere {len(full_data)} Kerzen im Cache: {cache_file}")
                if full_data.index.tz is None:
                    full_data.index = full_data.index.tz_localize('UTC')
                else:
                    full_data.index = full_data.index.tz_convert('UTC')
                full_data.to_csv(cache_file)
                req_start = pd.to_datetime(start_date_str, utc=True)
                req_end = pd.to_datetime(end_date_str + 'T23:59:59Z', utc=True)
                return full_data.loc[req_start:req_end].copy()
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

    logger.error(f"Konnte Daten für {symbol} ({timeframe}) weder aus Cache laden noch herunterladen.")
    return pd.DataFrame()

# --- NEUER BACKTESTER FÜR ENVELOPE ---
def run_envelope_backtest(data, params, start_capital=1000):
    """
    Führt einen Backtest für die Envelope-Strategie durch. Verwendet risikobasierte Positionsgrößen.
    """
    if data.empty:
        logger.warning("Leeres DataFrame an Backtester übergeben.")
        return {"total_pnl_pct": 0, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": start_capital}

    # --- Parameter extrahieren ---
    strategy_params = params['strategy']
    risk_params = params['risk']
    behavior_params = params['behavior']

    leverage = risk_params['leverage']
    # NEU: Risiko pro Entry Layer statt Balance Fraction
    risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Standard 0.5%
    num_envelopes = len(strategy_params['envelopes'])
    stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0 # Als Dezimalzahl
    use_longs = behavior_params.get('use_longs', True)
    use_shorts = behavior_params.get('use_shorts', True)

    fee_pct = 0.0006 # Beispiel: 0.06% Maker/Taker Fee

    # --- Indikatoren berechnen ---
    try:
        df, _ = calculate_indicators_and_signals(data.copy(), params)
        if df.empty:
            raise ValueError("Indikatorberechnung ergab leeres DataFrame.")
    except Exception as e:
        logger.warning(f"Fehler bei Indikatorberechnung im Backtest: {e}")
        return {"total_pnl_pct": -1000, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0}

    # --- Initialisierung für den Backtest-Loop ---
    capital = start_capital
    peak_capital = start_capital
    max_drawdown_pct = 0.0

    positions = [] # [{entry_price, amount_coins, side, sl_price, tp_price}, ...]
    closed_trades = [] # [{pnl, side}, ...]

    for i in range(len(df)):
        current_candle = df.iloc[i]
        capital_snapshot_start_of_candle = capital

        # --- Ausstiege prüfen (TP und SL) ---
        remaining_positions = []
        exit_pnl_current_candle = 0.0

        for pos in positions:
            exited = False
            exit_price = None
            pnl = 0.0
            pos_side = pos['side']
            pos_entry = pos['entry_price']
            pos_sl = pos['sl_price']
            pos_amount = pos['amount_coins']

            # SL Prüfung
            if pos_side == 'long' and current_candle['low'] <= pos_sl:
                exit_price = pos_sl
                exited = True
            elif pos_side == 'short' and current_candle['high'] >= pos_sl:
                exit_price = pos_sl
                exited = True

            # TP Prüfung (nur wenn SL nicht getroffen)
            if not exited:
                tp_price_current = current_candle['average']
                if pos_side == 'long' and current_candle['high'] >= tp_price_current:
                     if current_candle['open'] >= tp_price_current or current_candle['low'] <= tp_price_current:
                        exit_price = tp_price_current
                        exited = True
                elif pos_side == 'short' and current_candle['low'] <= tp_price_current:
                     if current_candle['open'] <= tp_price_current or current_candle['high'] >= tp_price_current:
                        exit_price = tp_price_current
                        exited = True

            # Wenn Ausstieg, PnL berechnen und Position entfernen
            if exited and exit_price is not None:
                if pos_side == 'long':
                    pnl = (exit_price - pos_entry) * pos_amount * leverage
                else: # short
                    pnl = (pos_entry - exit_price) * pos_amount * leverage

                # Gebühren abziehen
                entry_notional_value = pos_entry * pos_amount * leverage
                exit_notional_value = exit_price * pos_amount * leverage
                fees = (entry_notional_value * fee_pct) + (exit_notional_value * fee_pct)
                pnl -= fees

                exit_pnl_current_candle += pnl
                closed_trades.append({'pnl': pnl, 'side': pos_side})
            else:
                remaining_positions.append(pos) # Position bleibt offen

        positions = remaining_positions
        capital += exit_pnl_current_candle # Realisiertes Kapital nach Ausstiegen aktualisieren

        # --- Einstiege prüfen ---
        if capital > 0: # Nur wenn Kapital verfügbar
            # Long Entries
            if use_longs:
                side = 'long'
                for k in range(1, num_envelopes + 1):
                    low_band_col = f'band_low_{k}'
                    if low_band_col not in current_candle: continue
                    entry_trigger_price = current_candle[low_band_col]

                    if current_candle['low'] <= entry_trigger_price:
                        entry_price = entry_trigger_price # Vereinfacht: Einstieg zum Triggerpreis
                        if entry_price > 0:
                            # NEUE POSITIONSGRÖSSENBERECHNUNG
                            risk_amount_usd = capital * (risk_per_entry_pct / 100.0)
                            if risk_amount_usd <= 0: continue
                            sl_price = entry_price * (1 - stop_loss_pct_param)
                            sl_distance_price = abs(entry_price - sl_price)
                            if sl_distance_price <= 0: continue
                            amount_coins = risk_amount_usd / sl_distance_price
                            margin_required = (amount_coins * entry_price) / leverage
                            if margin_required > capital * 1.5: continue # Sicherheitscheck: Nicht mehr als 150% des Kapitals als Margin nutzen
                            # ENDE NEUE BERECHNUNG

                            tp_price = current_candle['average'] # TP ist der aktuelle Durchschnitt

                            positions.append({
                                'entry_price': entry_price,
                                'amount_coins': amount_coins, # Verwende neue Menge
                                'side': side,
                                'sl_price': sl_price,
                                'tp_price': tp_price
                            })

            # Short Entries
            if use_shorts:
                side = 'short'
                for k in range(1, num_envelopes + 1):
                    high_band_col = f'band_high_{k}'
                    if high_band_col not in current_candle: continue
                    entry_trigger_price = current_candle[high_band_col]

                    if current_candle['high'] >= entry_trigger_price:
                        entry_price = entry_trigger_price
                        if entry_price > 0:
                            # NEUE POSITIONSGRÖSSENBERECHNUNG
                            risk_amount_usd = capital * (risk_per_entry_pct / 100.0)
                            if risk_amount_usd <= 0: continue
                            sl_price = entry_price * (1 + stop_loss_pct_param)
                            sl_distance_price = abs(entry_price - sl_price)
                            if sl_distance_price <= 0: continue
                            amount_coins = risk_amount_usd / sl_distance_price
                            margin_required = (amount_coins * entry_price) / leverage
                            if margin_required > capital * 1.5: continue # Sicherheitscheck
                            # ENDE NEUE BERECHNUNG

                            tp_price = current_candle['average']

                            positions.append({
                                'entry_price': entry_price,
                                'amount_coins': amount_coins, # Verwende neue Menge
                                'side': side,
                                'sl_price': sl_price,
                                'tp_price': tp_price
                            })

        # --- Drawdown berechnen ---
        if capital_snapshot_start_of_candle > 0:
            current_drawdown = (peak_capital - capital_snapshot_start_of_candle) / peak_capital if peak_capital > 0 else 0
            max_drawdown_pct = max(max_drawdown_pct, current_drawdown * 100)

        peak_capital = max(peak_capital, capital)

        # --- Abbruch bei Totalverlust ---
        if capital <= 0:
            logger.warning(f"Reales Kapital <= 0 ({capital:.2f}). Backtest abgebrochen bei Kerze {i}.")
            capital = 0
            break

    # --- Endauswertung ---
    final_unrealized_pnl = 0.0
    if not df.empty:
        last_close_price = df.iloc[-1]['close']
        for pos in positions:
             leverage_pos = risk_params['leverage'] # Hebel holen
             if pos['side'] == 'long':
                 final_unrealized_pnl += (last_close_price - pos['entry_price']) * pos['amount_coins'] * leverage_pos
             else: # short
                 final_unrealized_pnl += (pos['entry_price'] - last_close_price) * pos['amount_coins'] * leverage_pos

    final_equity = max(0, capital + final_unrealized_pnl)
    total_pnl = final_equity - start_capital
    total_pnl_pct = (total_pnl / start_capital) * 100 if start_capital != 0 else 0
    trades_count = len(closed_trades)
    wins_count = sum(1 for trade in closed_trades if trade['pnl'] > 0)
    win_rate = (wins_count / trades_count) * 100 if trades_count > 0 else 0

    results = {
        "total_pnl_pct": round(total_pnl_pct, 2),
        "trades_count": trades_count,
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "end_capital": round(final_equity, 2),
        "start_capital": start_capital
    }
    return results
