# src/ltbbp/analysis/portfolio_simulator.py
import pandas as pd
import numpy as np
from tqdm import tqdm
import logging
import os
import sys

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# Import necessary functions
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

# --- KONSTANTEN FÜR REALISTISCHERE SIMULATION (Anpassen!) ---
# Werte sollten mit backtester.py übereinstimmen
SLIPPAGE_PCT_PER_TRADE = 0.0005 # Beispiel: 0.05% Slippage pro Ausführung (Market Order TP/SL)
MAX_TOTAL_POSITION_SIZE_USD_PER_STRATEGY = 50000 # Beispiel: Max. gehebelte Positionsgröße PRO STRATEGIE in USDT
# --- ENDE KONSTANTEN ---


def run_portfolio_simulation(start_capital, strategies_data, start_date, end_date):
    """
    Führt eine chronologische Portfolio-Simulation mit mehreren Envelope-Strategien durch.
    VERWENDET RISIKOBASIERTE POSITIONSGRÖSSENBERECHNUNG (BASIEREND AUF STARTKAPITAL),
    MARGIN TRACKING, SLIPPAGE und MAX POSITION SIZE PRO STRATEGIE.
    """
    logger.info("\n--- Starte Portfolio-Simulation (RISIKO, MARGIN, SLIPPAGE, MAX_POS)... ---")

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
            # Stelle sicher, dass 'params' existiert
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

    sorted_timestamps = sorted(list(all_timestamps))
    sim_start_ts = pd.to_datetime(start_date + " 00:00:00+00:00", utc=True)
    sim_end_ts = pd.to_datetime(end_date + " 23:59:59+00:00", utc=True)
    simulation_timestamps = [ts for ts in sorted_timestamps if sim_start_ts <= ts <= sim_end_ts]

    if not simulation_timestamps:
        logger.error("Keine gültigen Zeitstempel im Simulationszeitraum gefunden.")
        return None

    logger.info(f"Zeitraum: {simulation_timestamps[0]} bis {simulation_timestamps[-1]}")
    logger.info("2/4: Starte chronologische Simulation...")

    # --- Simulationsvariablen initialisieren ---
    equity = start_capital # Aktuelles REALISIERTES Kapital
    total_margin_used = 0.0 # Verfolgt die gesamte verwendete Margin über alle Strategien
    liquidation_date = None

    # Verfolgt offene Positionen pro Strategie
    open_portfolio_positions = {strategy_id: [] for strategy_id in strategy_dfs.keys()}
    # Verfolgt PnL und Drawdown
    closed_trades_portfolio = []
    equity_curve = [] # Speichert {'timestamp': ts, 'equity': total_equity_at_candle_start}
    peak_equity_curve = start_capital
    max_drawdown_pct = 0.0
    max_drawdown_date = None
    min_equity_during_sim = start_capital

    fee_pct = 0.0006

    # --- Simulations-Loop ---
    for ts in tqdm(simulation_timestamps, desc="Simuliere Portfolio"):
        if liquidation_date: break

        # --- Unrealisierten PnL zu Beginn der Kerze berechnen ---
        unrealized_pnl_start = 0.0
        for strategy_id_pnl, open_layers_pnl in open_portfolio_positions.items():
             if strategy_id_pnl not in strategy_dfs or ts not in strategy_dfs[strategy_id_pnl].index: continue
             current_candle_pnl = strategy_dfs[strategy_id_pnl].loc[ts]
             current_price_for_pnl = current_candle_pnl['open']
             for layer_pnl_calc in open_layers_pnl:
                 pos_lev_pnl = layer_pnl_calc.get('leverage', 1)
                 pos_amount_pnl = layer_pnl_calc['amount_coins']
                 pos_entry_pnl = layer_pnl_calc['entry_price']
                 layer_pnl_val = 0
                 if layer_pnl_calc['side'] == 'long':
                     layer_pnl_val = (current_price_for_pnl - pos_entry_pnl) * pos_amount_pnl * pos_lev_pnl
                 else: # short
                     layer_pnl_val = (pos_entry_pnl - current_price_for_pnl) * pos_amount_pnl * pos_lev_pnl
                 unrealized_pnl_start += layer_pnl_val

        total_equity_at_candle_start = equity + unrealized_pnl_start
        equity_curve.append({'timestamp': ts, 'equity': total_equity_at_candle_start})

        # --- Liquidation / Drawdown Check zu Beginn ---
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
            total_margin_used = 0
            # Fülle Rest der Equity Curve
            remaining_timestamps = [t for t in simulation_timestamps if t > ts]
            for rem_ts in remaining_timestamps:
                 equity_curve.append({'timestamp': rem_ts, 'equity': 0.0})
            break # Simulation beenden

        # --- Ausstiege prüfen ---
        total_exit_pnl_this_step = 0.0
        margin_freed_this_step = 0.0

        for strategy_id, open_layers in open_portfolio_positions.items():
            if strategy_id not in strategy_dfs or ts not in strategy_dfs[strategy_id].index: continue
            current_candle = strategy_dfs[strategy_id].loc[ts]
            remaining_layers = []

            for layer in open_layers:
                exited = False
                exit_price = None
                pnl = 0.0
                leverage = layer.get('leverage', 1)
                pos_side = layer['side']
                pos_entry = layer['entry_price']
                pos_sl = layer['sl_price']
                pos_amount_coins = layer['amount_coins']

                # SL Prüfung
                if pos_side == 'long' and current_candle['low'] <= pos_sl:
                    exit_price = pos_sl; exited = True
                elif pos_side == 'short' and current_candle['high'] >= pos_sl:
                    exit_price = pos_sl; exited = True

                # TP Prüfung
                if not exited:
                    tp_price_current = current_candle['average']
                    if pos_side == 'long' and current_candle['high'] >= tp_price_current:
                        if current_candle['open'] >= tp_price_current or current_candle['low'] <= tp_price_current:
                            exit_price = tp_price_current; exited = True
                    elif pos_side == 'short' and current_candle['low'] <= tp_price_current:
                        if current_candle['open'] <= tp_price_current or current_candle['high'] >= tp_price_current:
                            exit_price = tp_price_current; exited = True

                # PnL berechnen, Margin freigeben, Slippage anwenden
                if exited and exit_price is not None:
                    if pos_side == 'long':
                        pnl = (exit_price - pos_entry) * pos_amount_coins * leverage
                    else: # short
                        pnl = (pos_entry - exit_price) * pos_amount_coins * leverage

                    entry_value = pos_entry * pos_amount_coins * leverage
                    exit_value = exit_price * pos_amount_coins * leverage
                    fees = (entry_value * fee_pct) + (exit_value * fee_pct)
                    pnl -= fees

                    # *** NEU: Slippage hinzufügen ***
                    slippage_cost = abs(exit_value * SLIPPAGE_PCT_PER_TRADE)
                    pnl -= slippage_cost

                    total_exit_pnl_this_step += pnl
                    closed_trades_portfolio.append({'pnl': pnl, 'side': pos_side, 'strategy_id': strategy_id})

                    # Margin dieses Layers freigeben
                    margin_freed = (pos_entry * pos_amount_coins) / leverage
                    margin_freed_this_step += margin_freed
                else:
                    remaining_layers.append(layer) # Position bleibt offen

            open_portfolio_positions[strategy_id] = remaining_layers

        # Realisiertes Kapital und verwendete Margin nach allen Ausstiegen aktualisieren
        equity += total_exit_pnl_this_step
        total_margin_used -= margin_freed_this_step
        total_margin_used = max(0, total_margin_used)

        # --- Einstiege prüfen ---
        if equity > 0: # Nur wenn Kapital verfügbar
            available_equity_for_margin = equity - total_margin_used # Verfügbares Equity für neue Margin

            for strategy_id, strat_df in strategy_dfs.items():
                if ts not in strat_df.index: continue
                current_candle = strat_df.loc[ts]
                params = strategies_data[strategy_id]['params']
                strategy_params = params['strategy']
                risk_params = params['risk']
                behavior_params = params['behavior']
                leverage = risk_params['leverage']
                num_envelopes = len(strategy_params['envelopes'])
                stop_loss_pct_param = risk_params['stop_loss_pct'] / 100.0
                risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5)

                # *** NEU: Aktuellen Positionswert DIESER Strategie berechnen ***
                current_strat_pos_value_usd = 0.0
                current_price_for_strat_limit = current_candle['close']
                for existing_layer in open_portfolio_positions[strategy_id]:
                     lev_limit_strat = existing_layer.get('leverage', 1)
                     current_strat_pos_value_usd += existing_layer['amount_coins'] * current_price_for_strat_limit * lev_limit_strat

                # Long Entries
                if behavior_params.get('use_longs', True):
                    side = 'long'
                    for k in range(1, num_envelopes + 1):
                        low_band_col = f'band_low_{k}'
                        if low_band_col not in current_candle: continue
                        entry_trigger_price = current_candle[low_band_col]

                        if current_candle['low'] <= entry_trigger_price:
                            entry_price = entry_trigger_price
                            if entry_price > 0:
                                risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0) # Basiert auf Startkapital
                                if risk_amount_usd <= 0: continue
                                sl_price = entry_price * (1 - stop_loss_pct_param)
                                sl_distance_price = abs(entry_price - sl_price)
                                if sl_distance_price <= 0: continue
                                amount_coins = risk_amount_usd / sl_distance_price

                                # Max Position Size Check FÜR DIESE STRATEGIE
                                new_layer_value_usd = amount_coins * entry_price * leverage
                                if (current_strat_pos_value_usd + new_layer_value_usd) > MAX_TOTAL_POSITION_SIZE_USD_PER_STRATEGY:
                                     # logger.debug(f"Sim Portfolio Long {strategy_id} Layer {k}: Skip wg Max Strat Pos Size")
                                     continue

                                # Margin Check
                                margin_required = (amount_coins * entry_price) / leverage
                                if margin_required > available_equity_for_margin:
                                     # logger.warning(f"Sim Portfolio: Insufficient AVAILABLE equity ({available_equity_for_margin:.2f}) for margin ({margin_required:.2f}) on long {strategy_id}. Skipping.")
                                     continue # Diesen Layer überspringen, aber andere Strategien weiter prüfen

                                # Layer hinzufügen und verfügbare Margin / Strat Pos Value aktualisieren
                                tp_price = current_candle['average']
                                open_portfolio_positions[strategy_id].append({
                                    'entry_price': entry_price, 'amount_coins': amount_coins, 'side': side,
                                    'sl_price': sl_price, 'tp_price': tp_price, 'leverage': leverage
                                })
                                total_margin_used += margin_required
                                available_equity_for_margin -= margin_required
                                current_strat_pos_value_usd += new_layer_value_usd


                # Short Entries
                if behavior_params.get('use_shorts', True):
                    side = 'short'
                    for k in range(1, num_envelopes + 1):
                        high_band_col = f'band_high_{k}'
                        if high_band_col not in current_candle: continue
                        entry_trigger_price = current_candle[high_band_col]

                        if current_candle['high'] >= entry_trigger_price:
                            entry_price = entry_trigger_price
                            if entry_price > 0:
                                risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0) # Basiert auf Startkapital
                                if risk_amount_usd <= 0: continue
                                sl_price = entry_price * (1 + stop_loss_pct_param)
                                sl_distance_price = abs(entry_price - sl_price)
                                if sl_distance_price <= 0: continue
                                amount_coins = risk_amount_usd / sl_distance_price

                                # Max Position Size Check FÜR DIESE STRATEGIE
                                new_layer_value_usd = amount_coins * entry_price * leverage
                                if (current_strat_pos_value_usd + new_layer_value_usd) > MAX_TOTAL_POSITION_SIZE_USD_PER_STRATEGY:
                                     # logger.debug(f"Sim Portfolio Short {strategy_id} Layer {k}: Skip wg Max Strat Pos Size")
                                     continue

                                # Margin Check
                                margin_required = (amount_coins * entry_price) / leverage
                                if margin_required > available_equity_for_margin:
                                     # logger.warning(f"Sim Portfolio: Insufficient AVAILABLE equity ({available_equity_for_margin:.2f}) for margin ({margin_required:.2f}) on short {strategy_id}. Skipping.")
                                     continue

                                # Layer hinzufügen
                                tp_price = current_candle['average']
                                open_portfolio_positions[strategy_id].append({
                                    'entry_price': entry_price, 'amount_coins': amount_coins, 'side': side,
                                    'sl_price': sl_price, 'tp_price': tp_price, 'leverage': leverage
                                })
                                total_margin_used += margin_required
                                available_equity_for_margin -= margin_required
                                current_strat_pos_value_usd += new_layer_value_usd


    # --- Endauswertung ---
    logger.info("3/4: Bereite Analyse-Ergebnisse vor...")
    # Finales Equity ist der letzte Wert der Equity Curve
    final_equity_curve_val = equity_curve[-1]['equity'] if equity_curve else start_capital
    final_equity_curve_val = max(0, final_equity_curve_val) # Sicherstellen, nicht negativ

    total_pnl_pct = (final_equity_curve_val / start_capital - 1) * 100 if start_capital > 0 else 0
    trade_count = len(closed_trades_portfolio)
    wins = sum(1 for t in closed_trades_portfolio if t['pnl'] > 0)
    win_rate = (wins / trade_count * 100) if trade_count > 0 else 0

    pnl_per_strategy_df = pd.DataFrame(closed_trades_portfolio).groupby('strategy_id')['pnl'].sum().reset_index() if closed_trades_portfolio else pd.DataFrame(columns=['strategy_id', 'pnl'])
    trades_per_strategy_df = pd.DataFrame(closed_trades_portfolio).groupby('strategy_id').size().reset_index(name='trades') if closed_trades_portfolio else pd.DataFrame(columns=['strategy_id', 'trades'])

    equity_df = pd.DataFrame(equity_curve)
    # Neuberechnung des Drawdowns aus dem DataFrame am Ende (konsistenter)
    calculated_max_dd_pct_final = 0.0
    calculated_max_dd_date_final = None
    if not equity_df.empty:
        equity_df.set_index('timestamp', inplace=True)
        equity_df['peak'] = equity_df['equity'].cummax()
        equity_df['drawdown_pct'] = ((equity_df['peak'] - equity_df['equity']) / equity_df['peak'].replace(0, np.nan)).fillna(0) * 100
        if not equity_df['drawdown_pct'].empty:
            max_dd_idx = equity_df['drawdown_pct'].idxmax()
            if pd.notna(max_dd_idx):
                 max_dd_row = equity_df.loc[max_dd_idx]
                 calculated_max_dd_date_final = max_dd_idx # Ist bereits der Timestamp
                 calculated_max_dd_pct_final = max_dd_row['drawdown_pct']


    logger.info("4/4: Portfolio-Simulation abgeschlossen.")

    return {
        "start_capital": start_capital,
        "end_capital": final_equity_curve_val,
        "total_pnl_pct": total_pnl_pct,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "max_drawdown_pct": calculated_max_dd_pct_final, # Verwende den neu berechneten Wert
        "max_drawdown_date": calculated_max_dd_date_final, # Verwende das neu berechnete Datum
        "min_equity": min_equity_during_sim,
        "liquidation_date": liquidation_date,
        "pnl_per_strategy": pnl_per_strategy_df,
        "trades_per_strategy": trades_per_strategy_df,
        "equity_curve": equity_df # Gib das DataFrame mit DD% zurück
    }
