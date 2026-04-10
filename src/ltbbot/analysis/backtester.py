# src/ltbbot/analysis/backtester.py
import os
import pandas as pd
import numpy as np
from datetime import timedelta
import json
import sys
import logging
import ta as _ta

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange # Für load_data
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

# --- KONSTANTEN FÜR REALISTISCHERE SIMULATION ---
SLIPPAGE_PCT_EXIT  = 0.0005  # 0.05% Slippage auf Exit (Market Order TP/SL)
SLIPPAGE_PCT_ENTRY = 0.0012  # 0.12% Slippage auf Entry (Trigger-Limit, beobachtet live: +0.10–0.20%)
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
            # Prüfe, ob 'ltbbot' Key existiert und eine Liste ist
            if 'ltbbot' not in secrets or not isinstance(secrets['ltbbot'], list) or not secrets['ltbbot']:
                 raise ValueError("Kein gültiger 'ltbbot'-Eintrag in secret.json gefunden.")
            api_setup = secrets['ltbbot'][0]
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
        except (IndexError, KeyError, ValueError) as e: # Fängt Fehler bei ungültigem secret.json Format ab
            logger.error(f"Fehlerhafter oder fehlender Account-Eintrag ('ltbbot') in secret.json: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Fehler beim Daten-Download für {symbol} ({timeframe}): {e}", exc_info=True)
            return pd.DataFrame()

    logger.error(f"Konnte Daten für {symbol} ({timeframe}) weder aus Cache laden noch herunterladen.")
    return pd.DataFrame()

# --- NEUER BACKTESTER FÜR ENVELOPE (MIT KORREKTUREN) ---
def run_envelope_backtest(data, params, start_capital=1000, show_progress=True):
    """
    Führt einen Backtest für die Envelope-Strategie durch.
    KORRIGIERT: Verwendet Startkapital für Positionsgrößen, simuliert Slippage und Max Position Size.
    """
    if data.empty:
        logger.warning("Leeres DataFrame an Backtester übergeben.")
        # Rückgabeformat beibehalten
        return {"total_pnl_pct": -100, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}

    # --- Parameter extrahieren ---
    # Fange potenzielle KeyError ab, falls Config unvollständig ist
    try:
        strategy_params = params['strategy']
        risk_params = params['risk']
        behavior_params = params['behavior']

        leverage = risk_params['leverage']
        risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Risiko pro Layer
        num_envelopes = len(strategy_params['envelopes'])
        stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0 # Als Dezimalzahl
        use_longs = behavior_params.get('use_longs', True)
        use_shorts = behavior_params.get('use_shorts', True)
    except KeyError as e:
         logger.error(f"Fehlender Schlüssel in Parameter-Dict: {e}. Backtest abgebrochen.")
         return {"total_pnl_pct": -1000, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}


    fee_pct = 0.0006 # Beispiel: 0.06% Maker/Taker Fee

    # --- Indikatoren berechnen ---
    try:
        df, band_prices = calculate_indicators_and_signals(data.copy(), params)
        if df.empty:
            raise ValueError("Indikatorberechnung ergab leeres DataFrame.")
        
        # Extrahiere Regime-Informationen für spätere Verwendung
        # Die calculate_indicators_and_signals Funktion gibt band_prices mit regime/trend_direction zurück
        # Aber da wir Kerze für Kerze durchgehen, müssen wir das Regime für jede Kerze neu bestimmen
        # Das Regime ist bereits im df als Spalten vorhanden (falls detect_market_regime es setzt)
        # Alternativ: Wir berechnen es pro Kerze neu im Loop
        
    except Exception as e:
        logger.warning(f"Fehler bei Indikatorberechnung im Backtest: {e}")
        # Rückgabeformat beibehalten
        return {"total_pnl_pct": -1000, "trades_count": 0, "win_rate": 0, "max_drawdown_pct": 100, "end_capital": 0, "start_capital": start_capital}

    # --- Initialisierung für den Backtest-Loop ---
    capital = start_capital # Aktuelles REALISIERTES Kapital
    # peak_capital = start_capital # Höchststand inkl. unreal. PnL (wird jetzt aus equity_curve berechnet)
    # max_drawdown_pct = 0.0 # (wird jetzt aus equity_curve berechnet)

    positions = [] # [{entry_price, amount_coins, side, sl_price, tp_price, leverage}, ...]
    closed_trades = [] # [{pnl, side}, ...]
    equity_curve_data = [] # Für Drawdown-Berechnung am Ende
    
    # Starte Equity Curve mit Start Capital
    if not df.empty:
        first_timestamp = df.index[0]
        equity_curve_data.append({'timestamp': first_timestamp, 'equity': start_capital})
    
    # Regime-Indikatoren einmalig vorab berechnen (O(n) statt O(n²))
    _adx_pre   = _ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    _sma20_pre = _ta.trend.sma_indicator(df['close'], window=20)
    _sma50_pre = _ta.trend.sma_indicator(df['close'], window=50)
    _atr_pre   = _ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=10)
    _hl2_pre   = (df['high'] + df['low']) / 2
    _upper_pre = _hl2_pre + (3.0 * _atr_pre)
    _lower_pre = _hl2_pre - (3.0 * _atr_pre)

    # Progress Bar Setup
    total_candles = len(df)
    if show_progress:
        logger.info(f"Starte Backtest mit {total_candles} Kerzen...")
    progress_interval = max(1, total_candles // 20)  # 20 Updates (5% Schritte)

    for i in range(len(df)):
        # Progress Bar Update
        if show_progress and (i % progress_interval == 0 or i == total_candles - 1):
            progress_pct = (i + 1) / total_candles * 100
            bar_length = 30
            filled = int(bar_length * (i + 1) / total_candles)
            bar = '█' * filled + '░' * (bar_length - filled)
            print(f"\r  Progress: [{bar}] {progress_pct:.1f}% ({i+1}/{total_candles})", end='', flush=True)
        
        current_candle = df.iloc[i]
        timestamp = current_candle.name # Zeitstempel der Kerze


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
            # Dynamischer TP: aktueller MA jede Kerze (entspricht Live-Bot der TP jede Runde neu setzt)
            if not exited:
                tp_price_current = current_candle['average']
                if not pd.isna(tp_price_current) and tp_price_current > 0:
                    if pos_side == 'long' and current_candle['high'] >= tp_price_current:
                        if current_candle['open'] >= tp_price_current or current_candle['low'] <= tp_price_current:
                            exit_price = tp_price_current; exited = True
                    elif pos_side == 'short' and current_candle['low'] <= tp_price_current:
                        if current_candle['open'] <= tp_price_current or current_candle['high'] >= tp_price_current:
                            exit_price = tp_price_current; exited = True

            # Wenn Ausstieg, PnL berechnen und Position entfernen
            if exited and exit_price is not None and exit_price > 0: # Stelle sicher, dass Exit-Preis gültig ist
                if pos_side == 'long':
                    pnl = (exit_price - pos_entry) * pos_amount * pos_lev
                else: # short
                    pnl = (pos_entry - exit_price) * pos_amount * pos_lev

                # Gebühren abziehen
                entry_notional_value = pos_entry * pos_amount * pos_lev
                exit_notional_value = exit_price * pos_amount * pos_lev
                fees = (entry_notional_value * fee_pct) + (exit_notional_value * fee_pct)
                pnl -= fees

                # BUG3-FIX: Slippage auf Entry (Trigger-Limit) UND Exit (Market Order)
                entry_slippage_cost = abs(entry_notional_value * SLIPPAGE_PCT_ENTRY)
                exit_slippage_cost  = abs(exit_notional_value  * SLIPPAGE_PCT_EXIT)
                pnl -= entry_slippage_cost + exit_slippage_cost

                exit_pnl_current_candle += pnl
                closed_trades.append({'pnl': pnl, 'side': pos_side})
            else:
                remaining_positions.append(pos) # Position bleibt offen

        positions = remaining_positions
        capital += exit_pnl_current_candle # Realisiertes Kapital nach Ausstiegen aktualisieren
        
        # --- Equity Curve aktualisieren (nur bei Trade-Exit) ---
        if exit_pnl_current_candle != 0.0:
            equity_curve_data.append({'timestamp': timestamp, 'equity': capital})

        # --- Einstiege prüfen ---
        # Live Bot: Nur einsteigen wenn KEINE offene Position vorhanden
        if capital > 0 and len(positions) == 0:

            # Marktregime aus vorab berechneten Arrays lesen (O(1) statt O(n²))
            if i >= 49:
                _adx_v = _adx_pre.iloc[i]
                _cur_adx = float(_adx_v) if pd.notna(_adx_v) else 20.0
                _cur_price = df['close'].iloc[i]
                _cur_avg = current_candle.get('average', float('nan'))
                _price_dist = (abs(_cur_price - _cur_avg) / _cur_avg * 100
                               if (pd.notna(_cur_avg) and _cur_avg > 0) else 0.0)
                _f = _sma20_pre.iloc[i]; _s = _sma50_pre.iloc[i]
                _td = ("UPTREND" if (pd.notna(_f) and pd.notna(_s) and _s > 0 and _f > _s * 1.02)
                       else "DOWNTREND" if (pd.notna(_f) and pd.notna(_s) and _s > 0 and _f < _s * 0.98)
                       else "NEUTRAL")
                _u = _upper_pre.iloc[i]; _l = _lower_pre.iloc[i]
                _st = ("BULLISH" if (pd.notna(_u) and _cur_price > _u)
                       else "BEARISH" if (pd.notna(_l) and _cur_price < _l)
                       else "NEUTRAL")
                if _cur_adx > 30:
                    regime, trade_allowed, trend_direction = "STRONG_TREND", False, _td
                elif _cur_adx > 25:
                    regime, trade_allowed, trend_direction = "TREND", True, _td
                elif _cur_adx < 20 and _price_dist < 3:
                    regime, trade_allowed, trend_direction = "RANGE", True, "NEUTRAL"
                else:
                    regime, trade_allowed, trend_direction = "UNCERTAIN", True, _td
            else:
                regime, trade_allowed, trend_direction = "UNCERTAIN", True, "NEUTRAL"

            # STRONG_TREND: Kein Einstieg (wie Live Bot)
            if not trade_allowed:
                continue

            # Trend-Bias: Im Uptrend nur Longs, im Downtrend nur Shorts (wie Live Bot)
            current_use_longs = use_longs
            current_use_shorts = use_shorts
            if trend_direction == "UPTREND":
                current_use_shorts = False
            elif trend_direction == "DOWNTREND":
                current_use_longs = False

            # SL-Multiplikator: Im TREND 1.5x breiter (wie Live Bot)
            sl_multiplier = 1.5 if regime in ("TREND", "STRONG_TREND") else 1.0
            effective_sl_pct = stop_loss_pct_param * sl_multiplier

            # Risiko basiert auf STARTKAPITAL (statisch, kein Compounding - wie Live Bot)
            risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0)
            if risk_amount_usd <= 0:
                continue

            # Long Entry: ersten getriggerten Layer nehmen, dann stoppen (max. 1 Position)
            entered = False
            if current_use_longs:
                for k in range(1, num_envelopes + 1):
                    low_band_col = f'band_low_{k}'
                    if low_band_col not in current_candle or pd.isna(current_candle[low_band_col]) or current_candle[low_band_col] <= 0:
                        continue
                    entry_trigger_price = current_candle[low_band_col]
                    if not pd.isna(current_candle['low']) and current_candle['low'] <= entry_trigger_price:
                        entry_price = entry_trigger_price
                        sl_price = entry_price * (1 - effective_sl_pct)
                        if sl_price <= 0: continue
                        sl_distance_price = abs(entry_price - sl_price)
                        if sl_distance_price <= 0: continue
                        amount_coins = risk_amount_usd / sl_distance_price
                        # TP = MA beim Entry (wird jede Kerze dynamisch neu geprüft)
                        tp_price_target = current_candle['average']
                        positions.append({
                            'entry_price': entry_price,
                            'amount_coins': amount_coins,
                            'side': 'long',
                            'sl_price': sl_price,
                            'tp_price': tp_price_target,
                            'leverage': leverage
                        })
                        entered = True
                        break  # Nur EINEN Einstieg pro Kerze (wie Live Bot)

            # Short Entry: nur wenn kein Long-Einstieg erfolgte (max. 1 Position)
            if not entered and current_use_shorts:
                for k in range(1, num_envelopes + 1):
                    high_band_col = f'band_high_{k}'
                    if high_band_col not in current_candle or pd.isna(current_candle[high_band_col]) or current_candle[high_band_col] <= 0:
                        continue
                    entry_trigger_price = current_candle[high_band_col]
                    if not pd.isna(current_candle['high']) and current_candle['high'] >= entry_trigger_price:
                        entry_price = entry_trigger_price
                        sl_price = entry_price * (1 + effective_sl_pct)
                        if sl_price <= 0: continue
                        sl_distance_price = abs(entry_price - sl_price)
                        if sl_distance_price <= 0: continue
                        amount_coins = risk_amount_usd / sl_distance_price
                        # TP = MA beim Entry (wird jede Kerze dynamisch neu geprüft)
                        tp_price_target = current_candle['average']
                        positions.append({
                            'entry_price': entry_price,
                            'amount_coins': amount_coins,
                            'side': 'short',
                            'sl_price': sl_price,
                            'tp_price': tp_price_target,
                            'leverage': leverage
                        })
                        break  # Nur EINEN Einstieg pro Kerze (wie Live Bot)


        # --- Abbruch bei Totalverlust (basierend auf Equity Curve Start) ---
    
    # Progress Bar abschließen
    if show_progress:
        print()  # Newline nach Progress Bar
        logger.info("Backtest abgeschlossen. Berechne Metriken...")
    
    # --- Endauswertung ---
    final_equity = capital # KORREKTUR: Verwende die Variable 'capital'
    final_unrealized_pnl = 0.0
    if not df.empty and positions: # Nur wenn Positionen am Ende noch offen sind
        last_close_price = df['close'].iloc[-1]
        # Sicherstellen, dass der letzte Preis gültig ist
        if pd.isna(last_close_price) or last_close_price <= 0:
             logger.warning("Letzter Schlusspreis ungültig. Unrealisierter PnL am Ende könnte 0 sein.")
             # Fallback: Versuche letzten gültigen Close zu finden
             valid_closes = df['close'].dropna()
             last_close_price = valid_closes.iloc[-1] if not valid_closes.empty else 0

        if last_close_price > 0:
            for pos in positions:
                pos_lev_final = pos.get('leverage', 1)
                pos_amount_final = pos['amount_coins']
                pos_entry_final = pos['entry_price']
                if pos['side'] == 'long':
                    final_unrealized_pnl += (last_close_price - pos_entry_final) * pos_amount_final * pos_lev_final
                else: # short
                    final_unrealized_pnl += (pos_entry_final - last_close_price) * pos_amount_final * pos_lev_final

    final_total_equity = max(0, final_equity + final_unrealized_pnl) # Endgültiges Gesamtkapital

    # Füge den letzten Equity-Punkt hinzu (basierend auf Schlusskurs), falls Daten vorhanden
    if not df.empty:
         last_timestamp = df.index[-1]
         # Nur hinzufügen, wenn der Zeitstempel noch nicht existiert (verhindert Duplikate bei Abbruch)
         if not equity_curve_data or equity_curve_data[-1]['timestamp'] != last_timestamp:
              equity_curve_data.append({'timestamp': last_timestamp, 'equity': final_total_equity})

    total_pnl = final_total_equity - start_capital
    total_pnl_pct = (total_pnl / start_capital) * 100 if start_capital != 0 else 0
    trades_count = len(closed_trades)
    wins_count = sum(1 for trade in closed_trades if trade['pnl'] > 0)
    win_rate = (wins_count / trades_count) * 100 if trades_count > 0 else 0

    # Drawdown aus Equity Curve berechnen
    equity_df = pd.DataFrame(equity_curve_data)
    calculated_max_dd_pct = 0.0
    if not equity_df.empty:
        # Nur wenn Timestamp nicht bereits Index ist und Spalte existiert
        if 'timestamp' in equity_df.columns:
            # Duplikate im Timestamp entfernen, bevor Index gesetzt wird
            equity_df = equity_df.drop_duplicates(subset=['timestamp'], keep='last')
            equity_df.set_index('timestamp', inplace=True)
        # Überprüfe, ob 'equity' Spalte existiert
        if 'equity' in equity_df.columns:
            # Stelle sicher, dass Equity numerisch ist und fülle NaNs evtl. mit ffill
            equity_df['equity'] = pd.to_numeric(equity_df['equity'], errors='coerce')
            equity_df['equity'] = equity_df['equity'].ffill().fillna(start_capital) # Vorwärts füllen, dann mit Startkapital

            equity_df['peak'] = equity_df['equity'].cummax()
            # Vermeide Division durch Null oder NaNs im Peak
            peak_for_calc = equity_df['peak'].replace(0, np.nan)
            equity_df['drawdown_pct'] = ((peak_for_calc - equity_df['equity']) / peak_for_calc).fillna(0) * 100
            # Stelle sicher, dass DD nicht negativ wird (kann durch ffill passieren)
            equity_df['drawdown_pct'] = equity_df['drawdown_pct'].clip(lower=0)

            calculated_max_dd_pct = equity_df['drawdown_pct'].max() if not equity_df['drawdown_pct'].empty else 0.0
        else:
             logger.warning("Spalte 'equity' nicht im Equity DataFrame gefunden für Drawdown-Berechnung.")
    else:
        logger.warning("Equity Curve DataFrame ist leer. Drawdown kann nicht berechnet werden.")


    results = {
        "total_pnl_pct": round(total_pnl_pct, 2),
        "trades_count": trades_count,
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(calculated_max_dd_pct, 2), # Verwende berechneten DD
        "end_capital": round(final_total_equity, 2), # Verwende finales Gesamtkapital
        "start_capital": start_capital,
        "equity_curve": equity_curve_data  # Füge Equity Curve hinzu für Chart-Darstellung
    }
    return results
