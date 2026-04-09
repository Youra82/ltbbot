# src/ltbbot/analysis/portfolio_simulator.py
import pandas as pd
import numpy as np
import ta as _ta
from tqdm import tqdm
import logging
import os
import sys

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# Import necessary functions
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

# --- KONSTANTEN FÜR REALISTISCHERE SIMULATION ---
SLIPPAGE_PCT_PER_TRADE = 0.0005  # 0.05% Slippage pro Ausführung (Market Order TP/SL)
# --- ENDE KONSTANTEN ---


def run_portfolio_simulation(start_capital, strategies_data, start_date, end_date):
    """
    Führt eine chronologische Portfolio-Simulation mit mehreren Envelope-Strategien durch.
    EINHEITLICHE LOGIK mit backtester.py:
    - Max. 1 offene Position pro Strategie (wie Live Bot)
    - Statisches Startkapital für Risiko-Berechnung (kein Compounding)
    - SL 1.5x breiter im TREND (ADX 25-30)
    - Trend-Bias: Im Uptrend nur Longs, im Downtrend nur Shorts
    - Kein Trading bei STRONG_TREND (ADX > 30)
    """
    logger.info("\n--- Starte Portfolio-Simulation (Live-Bot-Logik, max. 1 Pos/Strategie)... ---")

    if not strategies_data:
        logger.error("Keine Strategie-Daten für die Simulation übergeben.")
        return None

    # --- Daten vorbereiten ---
    all_timestamps = set()
    strategy_dfs = {}
    processed_data_count = 0

    logger.info("1/4: Berechne Indikatoren für alle Strategien...")
    for strategy_id, strat_info in strategies_data.items():
        try:
            if 'params' not in strat_info:
                logger.error(f"Fehlende 'params' in strat_info für {strategy_id}. Überspringe.")
                continue
            df_with_indicators, _ = calculate_indicators_and_signals(strat_info['data'].copy(), strat_info['params'])
            if df_with_indicators.empty:
                logger.warning(f"Keine Indikatordaten für Strategie {strategy_id}. Wird ignoriert.")
                continue
            strategy_dfs[strategy_id] = df_with_indicators
            all_timestamps.update(df_with_indicators.index)
            processed_data_count += 1
        except Exception as e:
            logger.error(f"Fehler bei Indikatorberechnung für {strategy_id}: {e}. Wird ignoriert.")
            continue

    if processed_data_count == 0:
        logger.error("Konnte für keine Strategie Indikatoren berechnen. Breche Simulation ab.")
        return None

    # Regime-Indikatoren vorab berechnen (O(n) statt O(n²)) – wie backtester.py
    strategy_pre_indicators = {}
    for strategy_id, df in strategy_dfs.items():
        _atr = _ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=10)
        _hl2 = (df['high'] + df['low']) / 2
        strategy_pre_indicators[strategy_id] = {
            'adx':   _ta.trend.adx(df['high'], df['low'], df['close'], window=14),
            'sma20': _ta.trend.sma_indicator(df['close'], window=20),
            'sma50': _ta.trend.sma_indicator(df['close'], window=50),
            'upper': _hl2 + (3.0 * _atr),
            'lower': _hl2 - (3.0 * _atr),
        }

    sorted_timestamps = sorted(list(all_timestamps))
    sim_start_ts = pd.to_datetime(start_date + " 00:00:00+00:00", utc=True)
    sim_end_ts   = pd.to_datetime(end_date   + " 23:59:59+00:00", utc=True)
    simulation_timestamps = [ts for ts in sorted_timestamps if sim_start_ts <= ts <= sim_end_ts]

    if not simulation_timestamps:
        logger.error("Keine gültigen Zeitstempel im Simulationszeitraum gefunden.")
        return None

    logger.info(f"Zeitraum: {simulation_timestamps[0]} bis {simulation_timestamps[-1]}")
    logger.info("2/4: Starte chronologische Simulation...")

    # --- Simulationsvariablen initialisieren ---
    equity = start_capital
    liquidation_date = None

    open_portfolio_positions = {strategy_id: [] for strategy_id in strategy_dfs.keys()}
    closed_trades_portfolio = []
    equity_curve = []
    peak_equity_curve = start_capital
    max_drawdown_pct = 0.0
    max_drawdown_date = None
    min_equity_during_sim = start_capital

    fee_pct = 0.0006

    # --- Simulations-Loop ---
    for ts in tqdm(simulation_timestamps, desc="Simuliere Portfolio"):
        if liquidation_date:
            break

        # --- Unrealisierten PnL berechnen ---
        unrealized_pnl_start = 0.0
        for strategy_id_pnl, open_layers_pnl in open_portfolio_positions.items():
            if strategy_id_pnl not in strategy_dfs or ts not in strategy_dfs[strategy_id_pnl].index:
                continue
            current_candle_pnl = strategy_dfs[strategy_id_pnl].loc[ts]
            current_price_for_pnl = current_candle_pnl['open']
            for layer_pnl_calc in open_layers_pnl:
                pos_lev_pnl    = layer_pnl_calc.get('leverage', 1)
                pos_amount_pnl = layer_pnl_calc['amount_coins']
                pos_entry_pnl  = layer_pnl_calc['entry_price']
                if layer_pnl_calc['side'] == 'long':
                    unrealized_pnl_start += (current_price_for_pnl - pos_entry_pnl) * pos_amount_pnl * pos_lev_pnl
                else:
                    unrealized_pnl_start += (pos_entry_pnl - current_price_for_pnl) * pos_amount_pnl * pos_lev_pnl

        total_equity_at_candle_start = equity + unrealized_pnl_start
        equity_curve.append({'timestamp': ts, 'equity': total_equity_at_candle_start})

        # --- Liquidation / Drawdown Check ---
        peak_equity_curve = max(peak_equity_curve, total_equity_at_candle_start)
        current_drawdown = (peak_equity_curve - total_equity_at_candle_start) / peak_equity_curve if peak_equity_curve > 0 else 0
        current_dd_pct_val = current_drawdown * 100
        if current_dd_pct_val > max_drawdown_pct:
            max_drawdown_pct = current_dd_pct_val
            max_drawdown_date = ts

        min_equity_during_sim = min(min_equity_during_sim, total_equity_at_candle_start)

        if total_equity_at_candle_start <= 0 and not liquidation_date:
            liquidation_date = ts
            logger.warning(f"PORTFOLIO LIQUIDIERT (Sim Equity <= 0) am {ts.strftime('%Y-%m-%d')}!")
            equity = 0
            remaining_timestamps = [t for t in simulation_timestamps if t > ts]
            for rem_ts in remaining_timestamps:
                equity_curve.append({'timestamp': rem_ts, 'equity': 0.0})
            break

        # --- Ausstiege prüfen (SL / TP) ---
        total_exit_pnl_this_step = 0.0

        for strategy_id, open_layers in open_portfolio_positions.items():
            if strategy_id not in strategy_dfs or ts not in strategy_dfs[strategy_id].index:
                continue
            current_candle = strategy_dfs[strategy_id].loc[ts]
            remaining_layers = []

            for layer in open_layers:
                exited    = False
                exit_price = None
                pnl       = 0.0
                leverage      = layer.get('leverage', 1)
                pos_side      = layer['side']
                pos_entry     = layer['entry_price']
                pos_sl        = layer['sl_price']
                pos_amount    = layer['amount_coins']

                # SL
                if pos_side == 'long' and current_candle['low'] <= pos_sl:
                    exit_price = pos_sl; exited = True
                elif pos_side == 'short' and current_candle['high'] >= pos_sl:
                    exit_price = pos_sl; exited = True

                # TP
                if not exited:
                    tp_price_current = current_candle['average']
                    if not pd.isna(tp_price_current) and tp_price_current > 0:
                        if pos_side == 'long' and current_candle['high'] >= tp_price_current:
                            if current_candle['open'] >= tp_price_current or current_candle['low'] <= tp_price_current:
                                exit_price = tp_price_current; exited = True
                        elif pos_side == 'short' and current_candle['low'] <= tp_price_current:
                            if current_candle['open'] <= tp_price_current or current_candle['high'] >= tp_price_current:
                                exit_price = tp_price_current; exited = True

                if exited and exit_price is not None:
                    if pos_side == 'long':
                        pnl = (exit_price - pos_entry) * pos_amount * leverage
                    else:
                        pnl = (pos_entry - exit_price) * pos_amount * leverage

                    entry_value = pos_entry * pos_amount * leverage
                    exit_value  = exit_price * pos_amount * leverage
                    fees = (entry_value * fee_pct) + (exit_value * fee_pct)
                    pnl -= fees
                    pnl -= abs(exit_value * SLIPPAGE_PCT_PER_TRADE)

                    total_exit_pnl_this_step += pnl
                    entry_value_notional = pos_entry * pos_amount
                    pnl_pct = (pnl / (entry_value_notional / leverage * leverage)) * 100 if entry_value_notional > 0 else 0.0
                    reason = 'WIN' if pnl > 0 else 'SL'
                    closed_trades_portfolio.append({
                        'exit_time':    ts,
                        'entry_time':   layer.get('entry_time', ts),
                        'symbol':       strategies_data[strategy_id]['symbol'],
                        'timeframe':    strategies_data[strategy_id]['timeframe'],
                        'side':         pos_side,
                        'entry_price':  round(pos_entry, 6),
                        'exit_price':   round(exit_price, 6),
                        'sl_price':     round(pos_sl, 6),
                        'leverage':     leverage,
                        'pnl_usd':      round(pnl, 4),
                        'pnl_pct':      round(pnl_pct, 2),
                        'reason':       reason,
                        'strategy_id':  strategy_id,
                    })
                else:
                    remaining_layers.append(layer)

            open_portfolio_positions[strategy_id] = remaining_layers

        equity += total_exit_pnl_this_step

        # --- Einstiege prüfen ---
        # Wie Live Bot: Nur einsteigen wenn KEINE offene Position für diese Strategie
        if equity > 0:
            for strategy_id, strat_df in strategy_dfs.items():
                if ts not in strat_df.index:
                    continue

                # Max. 1 Position pro Strategie (wie Live Bot)
                if len(open_portfolio_positions[strategy_id]) > 0:
                    continue

                current_candle = strat_df.loc[ts]
                params         = strategies_data[strategy_id]['params']
                strategy_params = params['strategy']
                risk_params     = params['risk']
                behavior_params = params['behavior']
                leverage            = risk_params['leverage']
                num_envelopes       = len(strategy_params['envelopes'])
                stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0
                risk_per_entry_pct  = risk_params.get('risk_per_entry_pct', 0.5)
                use_longs  = behavior_params.get('use_longs', True)
                use_shorts = behavior_params.get('use_shorts', True)

                # Marktregime aus vorab berechneten Arrays lesen (O(1))
                pre = strategy_pre_indicators[strategy_id]
                try:
                    df_idx = strat_df.index.get_loc(ts)
                except KeyError:
                    continue

                if df_idx >= 49:
                    _adx_v    = pre['adx'].iloc[df_idx]
                    _cur_adx  = float(_adx_v) if pd.notna(_adx_v) else 20.0
                    _cur_price = current_candle['close']
                    _cur_avg  = current_candle.get('average', float('nan'))
                    _price_dist = (abs(_cur_price - _cur_avg) / _cur_avg * 100
                                   if (pd.notna(_cur_avg) and _cur_avg > 0) else 0.0)
                    _f = pre['sma20'].iloc[df_idx]
                    _s = pre['sma50'].iloc[df_idx]
                    _td = ("UPTREND"   if (pd.notna(_f) and pd.notna(_s) and _s > 0 and _f > _s * 1.02)
                           else "DOWNTREND" if (pd.notna(_f) and pd.notna(_s) and _s > 0 and _f < _s * 0.98)
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

                # STRONG_TREND: kein Einstieg (wie Live Bot)
                if not trade_allowed:
                    continue

                # Trend-Bias (wie Live Bot)
                current_use_longs  = use_longs
                current_use_shorts = use_shorts
                if trend_direction == "UPTREND":
                    current_use_shorts = False
                elif trend_direction == "DOWNTREND":
                    current_use_longs = False

                # SL-Multiplikator im TREND (wie Live Bot)
                sl_multiplier  = 1.5 if regime in ("TREND", "STRONG_TREND") else 1.0
                effective_sl_pct = stop_loss_pct_param * sl_multiplier

                # Risiko basiert auf STARTKAPITAL (statisch – wie Live Bot)
                risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0:
                    continue

                # Long Entry: ersten getriggerten Layer nehmen, dann stoppen
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
                            tp_price = current_candle['average']
                            open_portfolio_positions[strategy_id].append({
                                'entry_price': entry_price, 'amount_coins': amount_coins,
                                'side': 'long', 'sl_price': sl_price,
                                'tp_price': tp_price, 'leverage': leverage,
                                'entry_time': ts,
                            })
                            entered = True
                            break  # Nur EINEN Einstieg pro Kerze (wie Live Bot)

                # Short Entry: nur wenn kein Long-Einstieg erfolgte
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
                            tp_price = current_candle['average']
                            open_portfolio_positions[strategy_id].append({
                                'entry_price': entry_price, 'amount_coins': amount_coins,
                                'side': 'short', 'sl_price': sl_price,
                                'tp_price': tp_price, 'leverage': leverage,
                                'entry_time': ts,
                            })
                            break  # Nur EINEN Einstieg pro Kerze (wie Live Bot)

    # --- Endauswertung ---
    logger.info("3/4: Bereite Analyse-Ergebnisse vor...")
    final_equity_curve_val = equity_curve[-1]['equity'] if equity_curve else start_capital
    final_equity_curve_val = max(0, final_equity_curve_val)

    total_pnl_pct = (final_equity_curve_val / start_capital - 1) * 100 if start_capital > 0 else 0
    trade_count = len(closed_trades_portfolio)
    wins = sum(1 for t in closed_trades_portfolio if t['pnl_usd'] > 0)
    win_rate = (wins / trade_count * 100) if trade_count > 0 else 0

    trades_df = pd.DataFrame(closed_trades_portfolio) if closed_trades_portfolio else pd.DataFrame(
        columns=['exit_time','entry_time','symbol','timeframe','side','entry_price','exit_price','sl_price','leverage','pnl_usd','pnl_pct','reason','strategy_id'])

    pnl_per_strategy_df    = trades_df.groupby('strategy_id')['pnl_usd'].sum().reset_index().rename(columns={'pnl_usd':'pnl'}) if not trades_df.empty else pd.DataFrame(columns=['strategy_id', 'pnl'])
    trades_per_strategy_df = trades_df.groupby('strategy_id').size().reset_index(name='trades') if not trades_df.empty else pd.DataFrame(columns=['strategy_id', 'trades'])

    equity_df = pd.DataFrame(equity_curve)
    calculated_max_dd_pct_final  = 0.0
    calculated_max_dd_date_final = None
    if not equity_df.empty:
        equity_df.set_index('timestamp', inplace=True)
        equity_df['peak'] = equity_df['equity'].cummax()
        equity_df['drawdown_pct'] = ((equity_df['peak'] - equity_df['equity']) / equity_df['peak'].replace(0, np.nan)).fillna(0) * 100
        if not equity_df['drawdown_pct'].empty:
            max_dd_idx = equity_df['drawdown_pct'].idxmax()
            if pd.notna(max_dd_idx):
                calculated_max_dd_date_final = max_dd_idx
                calculated_max_dd_pct_final  = equity_df.loc[max_dd_idx, 'drawdown_pct']

    logger.info("4/4: Portfolio-Simulation abgeschlossen.")

    return {
        "start_capital":      start_capital,
        "end_capital":        final_equity_curve_val,
        "total_pnl_pct":      total_pnl_pct,
        "trade_count":        trade_count,
        "win_rate":           win_rate,
        "max_drawdown_pct":   calculated_max_dd_pct_final,
        "max_drawdown_date":  calculated_max_dd_date_final,
        "min_equity":         min_equity_during_sim,
        "liquidation_date":   liquidation_date,
        "pnl_per_strategy":   pnl_per_strategy_df,
        "trades_per_strategy": trades_per_strategy_df,
        "equity_curve":       equity_df,
        "trades_df":          trades_df,
    }
