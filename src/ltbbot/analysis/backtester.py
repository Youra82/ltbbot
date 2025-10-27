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

# --- KONSTANTEN FÜR REALISTISCHERE SIMULATION (Anpassen!) ---
SLIPPAGE_PCT_PER_TRADE = 0.0005 # Beispiel: 0.05% Slippage pro Ausführung (Market Order TP/SL)
MAX_TOTAL_POSITION_SIZE_USD = 50000 # Beispiel: Max. gehebelte Positionsgröße pro Symbol in USDT
# --- ENDE KONSTANTEN ---

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
                # Sicherstellen, dass nur der angeforderte Bereich zurückgegeben wird
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

# --- NEUER BACKTESTER FÜR ENVELOPE (MIT KORREKTUREN) ---
def run_envelope_backtest(data, params, start_capital=1000):
    """
    Führt einen Backtest für die Envelope-Strategie durch.
    KORRIGIERT: Verwendet Startkapital für Positionsgrößen, simuliert Slippage und Max Position Size.
    """
    if data.empty:
        logger.warning("Leeres DataFrame an Backtester übergeben.")
        # Rückgabeformat beibehalten
        return {"total_pnl_pct": -100, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}

    # --- Parameter extrahieren ---
    strategy_params = params['strategy']
    risk_params = params['risk']
    behavior_params = params['behavior']

    leverage = risk_params['leverage']
    risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Risiko pro Layer
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
        # Rückgabeformat beibehalten
        return {"total_pnl_pct": -1000, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}

    # --- Initialisierung für den Backtest-Loop ---
    capital = start_capital # Aktuelles REALISIERTES Kapital
    peak_capital = start_capital # Höchststand inkl. unreal. PnL
    max_drawdown_pct = 0.0

    positions = [] # [{entry_price, amount_coins, side, sl_price, tp_price, leverage}, ...]
    closed_trades = [] # [{pnl, side}, ...]
    equity_curve_data = [] # Für Drawdown-Berechnung am Ende

    for i in range(len(df)):
        current_candle = df.iloc[i]
        timestamp = current_candle.name # Zeitstempel der Kerze

        # --- Unrealisierten PnL zu Beginn der Kerze berechnen ---
        unrealized_pnl_start = 0.0
        current_portfolio_value_usd = 0.0 # Gehebelter Wert aller offenen Positionen
        for pos in positions:
            current_price_for_pnl = current_candle['open'] # PnL zu Beginn der Kerze
            pos_lev = pos.get('leverage', 1)
            pos_amount = pos['amount_coins']
            pos_entry = pos['entry_price']
            layer_pnl = 0
            if pos['side'] == 'long':
                layer_pnl = (current_price_for_pnl - pos_entry) * pos_amount * pos_lev
            else: # short
                layer_pnl = (pos_entry - current_price_for_pnl) * pos_amount * pos_lev
            unrealized_pnl_start += layer_pnl
            current_portfolio_value_usd += pos_amount * current_price_for_pnl * pos_lev

        equity_at_candle_start = capital + unrealized_pnl_start
        equity_curve_data.append({'timestamp': timestamp, 'equity': equity_at_candle_start})

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
            pos_lev = pos.get('leverage', 1) # Hebel holen

            # SL Prüfung
            if pos_side == 'long' and current_candle['low'] <= pos_sl:
                exit_price = pos_sl; exited = True
            elif pos_side == 'short' and current_candle['high'] >= pos_sl:
                exit_price = pos_sl; exited = True

            # TP Prüfung (nur wenn SL nicht getroffen)
            if not exited:
                tp_price_current = current_candle['average']
                if pos_side == 'long' and current_candle['high'] >= tp_price_current:
                    if current_candle['open'] >= tp_price_current or current_candle['low'] <= tp_price_current:
                        exit_price = tp_price_current; exited = True
                elif pos_side == 'short' and current_candle['low'] <= tp_price_current:
                    if current_candle['open'] <= tp_price_current or current_candle['high'] >= tp_price_current:
                        exit_price = tp_price_current; exited = True

            # Wenn Ausstieg, PnL berechnen und Position entfernen
            if exited and exit_price is not None:
                if pos_side == 'long':
                    pnl = (exit_price - pos_entry) * pos_amount * pos_lev
                else: # short
                    pnl = (pos_entry - exit_price) * pos_amount * pos_lev

                # Gebühren abziehen
                entry_notional_value = pos_entry * pos_amount * pos_lev
                exit_notional_value = exit_price * pos_amount * pos_lev
                fees = (entry_notional_value * fee_pct) + (exit_notional_value * fee_pct)
                pnl -= fees

                # *** NEU: Slippage hinzufügen (simuliert für Market Order TP/SL) ***
                slippage_cost = abs(exit_notional_value * SLIPPAGE_PCT_PER_TRADE)
                pnl -= slippage_cost

                exit_pnl_current_candle += pnl
                closed_trades.append({'pnl': pnl, 'side': pos_side})
            else:
                remaining_positions.append(pos) # Position bleibt offen

        positions = remaining_positions
        capital += exit_pnl_current_candle # Realisiertes Kapital nach Ausstiegen aktualisieren

        # --- Einstiege prüfen ---
        if capital > 0: # Nur wenn Kapital verfügbar
            # *** NEU: Aktuellen Gesamtwert der offenen Positionen berechnen für Limit-Check ***
            current_total_pos_value_usd = 0.0
            current_price_for_limit = current_candle['close'] # Aktueller Preis für Limit-Check
            for existing_pos in positions:
                 pos_lev_limit = existing_pos.get('leverage', 1)
                 current_total_pos_value_usd += existing_pos['amount_coins'] * current_price_for_limit * pos_lev_limit

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
                            # *** KORRIGIERTE POSITIONSGRÖSSENBERECHNUNG ***
                            risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0) # <--- BASIERT AUF STARTKAPITAL
                            if risk_amount_usd <= 0: continue

                            sl_price = entry_price * (1 - stop_loss_pct_param)
                            sl_distance_price = abs(entry_price - sl_price)
                            if sl_distance_price <= 0: continue

                            amount_coins = risk_amount_usd / sl_distance_price
                            # Mindestmenge prüfen (vereinfacht, echter Check wäre besser)
                            # if amount_coins < MIN_TRADE_AMOUNT_FROM_CONFIG: continue

                            # *** NEU: Max Position Size Check ***
                            new_layer_value_usd = amount_coins * entry_price * leverage
                            if (current_total_pos_value_usd + new_layer_value_usd) > MAX_TOTAL_POSITION_SIZE_USD:
                                # logger.debug(f"Sim Long Layer {k}: Überspringt wg Max Pos Size ({current_total_pos_value_usd + new_layer_value_usd:.0f} > {MAX_TOTAL_POSITION_SIZE_USD:.0f})")
                                continue # Diesen Layer überspringen

                            tp_price = current_candle['average']

                            positions.append({
                                'entry_price': entry_price,
                                'amount_coins': amount_coins,
                                'side': side,
                                'sl_price': sl_price,
                                'tp_price': tp_price, # TP Ziel speichern (obwohl es sich ändert)
                                'leverage': leverage # Hebel speichern
                            })
                            # Aktualisiere den Gesamtwert für nachfolgende Layer in dieser Kerze
                            current_total_pos_value_usd += new_layer_value_usd

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
                            # *** KORRIGIERTE POSITIONSGRÖSSENBERECHNUNG ***
                            risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0) # <--- BASIERT AUF STARTKAPITAL
                            if risk_amount_usd <= 0: continue

                            sl_price = entry_price * (1 + stop_loss_pct_param)
                            sl_distance_price = abs(entry_price - sl_price)
                            if sl_distance_price <= 0: continue

                            amount_coins = risk_amount_usd / sl_distance_price
                            # if amount_coins < MIN_TRADE_AMOUNT_FROM_CONFIG: continue

                            # *** NEU: Max Position Size Check ***
                            new_layer_value_usd = amount_coins * entry_price * leverage
                            if (current_total_pos_value_usd + new_layer_value_usd) > MAX_TOTAL_POSITION_SIZE_USD:
                                # logger.debug(f"Sim Short Layer {k}: Überspringt wg Max Pos Size ({current_total_pos_value_usd + new_layer_value_usd:.0f} > {MAX_TOTAL_POSITION_SIZE_USD:.0f})")
                                continue

                            tp_price = current_candle['average']

                            positions.append({
                                'entry_price': entry_price,
                                'amount_coins': amount_coins,
                                'side': side,
                                'sl_price': sl_price,
                                'tp_price': tp_price,
                                'leverage': leverage
                            })
                            current_total_pos_value_usd += new_layer_value_usd


        # --- Drawdown basierend auf Equity Curve (wird am Ende berechnet) ---

        # --- Abbruch bei Totalverlust (basierend auf Equity Curve Start) ---
        if equity_at_candle_start <= 0:
            logger.warning(f"Simuliertes Equity <= 0 ({equity_at_candle_start:.2f}). Backtest abgebrochen bei Kerze {i}.")
            capital = 0
            # Fülle Rest der Equity Curve mit 0
            remaining_indices = range(i + 1, len(df))
            for rem_idx in remaining_indices:
                 equity_curve_data.append({'timestamp': df.index[rem_idx], 'equity': 0.0})
            break

    # --- Endauswertung ---
    final_equity = equity # Realisiertes Kapital am Ende
    final_unrealized_pnl = 0.0
    if not df.empty and positions: # Nur wenn Positionen am Ende noch offen sind
        last_close_price = df.iloc[-1]['close']
        for pos in positions:
            pos_lev_final = pos.get('leverage', 1)
            pos_amount_final = pos['amount_coins']
            pos_entry_final = pos['entry_price']
            if pos['side'] == 'long':
                final_unrealized_pnl += (last_close_price - pos_entry_final) * pos_amount_final * pos_lev_final
            else: # short
                final_unrealized_pnl += (pos_entry_final - last_close_price) * pos_amount_final * pos_lev_final

    final_total_equity = max(0, final_equity + final_unrealized_pnl) # Endgültiges Gesamtkapital

    # Füge den letzten Equity-Punkt hinzu (basierend auf Schlusskurs)
    if not df.empty:
         equity_curve_data.append({'timestamp': df.index[-1], 'equity': final_total_equity})

    total_pnl = final_total_equity - start_capital
    total_pnl_pct = (total_pnl / start_capital) * 100 if start_capital != 0 else 0
    trades_count = len(closed_trades)
    wins_count = sum(1 for trade in closed_trades if trade['pnl'] > 0)
    win_rate = (wins_count / trades_count) * 100 if trades_count > 0 else 0

    # Drawdown aus Equity Curve berechnen
    equity_df = pd.DataFrame(equity_curve_data)
    calculated_max_dd_pct = 0.0
    if not equity_df.empty:
        equity_df.set_index('timestamp', inplace=True)
        equity_df['peak'] = equity_df['equity'].cummax()
        equity_df['drawdown_pct'] = ((equity_df['peak'] - equity_df['equity']) / equity_df['peak'].replace(0, np.nan)).fillna(0) * 100
        calculated_max_dd_pct = equity_df['drawdown_pct'].max() if not equity_df['drawdown_pct'].empty else 0.0

    results = {
        "total_pnl_pct": round(total_pnl_pct, 2),
        "trades_count": trades_count,
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(calculated_max_dd_pct, 2), # Verwende berechneten DD
        "end_capital": round(final_total_equity, 2), # Verwende finales Gesamtkapital
        "start_capital": start_capital
    }
    return results
