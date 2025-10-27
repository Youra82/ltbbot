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

def run_portfolio_simulation(start_capital, strategies_data, start_date, end_date):
    """
    Führt eine chronologische Portfolio-Simulation mit mehreren Envelope-Strategien durch.
    VERWENDET JETZT RISIKOBASIERTE POSITIONSGRÖSSENBERECHNUNG (BASIEREND AUF STARTKAPITAL)
    UND MARGIN TRACKING.
    """
    logger.info("\n--- Starte Portfolio-Simulation (RISIKOBASIERT + MARGIN TRACKING)... ---")

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
    equity = start_capital # Aktuelles Kapital (realisiert)
    peak_equity_curve = start_capital # Höchststand der Equity Curve (inkl. unreal. PnL)
    min_equity_during_sim = start_capital
    max_drawdown_pct = 0.0
    max_drawdown_date = None
    liquidation_date = None
    total_margin_used = 0.0 # NEU: Verfolgt die gesamte verwendete Margin

    open_portfolio_positions = {strategy_id: [] for strategy_id in strategy_dfs.keys()}
    closed_trades_portfolio = []
    equity_curve = []
    fee_pct = 0.0006

    # --- Simulations-Loop ---
    for ts in tqdm(simulation_timestamps, desc="Simuliere Portfolio"):
        if liquidation_date: break

        current_equity_realized = equity # Realisiertes Kapital zu Beginn des Zeitstempels
        total_exit_pnl_this_step = 0.0
        margin_freed_this_step = 0.0 # NEU: Verfolgt freigegebene Margin

        # --- 1. Ausstiege (SL & TP) für alle offenen Positionen prüfen ---
        for strategy_id, open_layers in open_portfolio_positions.items():
            if strategy_id not in strategy_dfs: continue
            strat_df = strategy_dfs[strategy_id]
            if ts not in strat_df.index: continue

            current_candle = strat_df.loc[ts]
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

                # PnL berechnen und Margin freigeben
                if exited and exit_price is not None:
                    if pos_side == 'long':
                        pnl = (exit_price - pos_entry) * pos_amount_coins * leverage
                    else: # short
                        pnl = (pos_entry - exit_price) * pos_amount_coins * leverage

                    entry_value = pos_entry * pos_amount_coins * leverage
                    exit_value = exit_price * pos_amount_coins * leverage
                    fees = (entry_value * fee_pct) + (exit_value * fee_pct)
                    pnl -= fees

                    total_exit_pnl_this_step += pnl
                    closed_trades_portfolio.append({'pnl': pnl, 'side': pos_side, 'strategy_id': strategy_id})

                    # NEU: Margin dieses Layers freigeben
                    margin_freed = (pos_entry * pos_amount_coins) / leverage
                    margin_freed_this_step += margin_freed
                else:
                    remaining_layers.append(layer) # Position bleibt offen

            open_portfolio_positions[strategy_id] = remaining_layers

        # Realisiertes Kapital und verwendete Margin nach allen Ausstiegen aktualisieren
        equity += total_exit_pnl_this_step
        total_margin_used -= margin_freed_this_step
        total_margin_used = max(0, total_margin_used) # Sicherstellen, dass Margin nicht negativ wird

        # --- 2. Einstiege für alle Strategien prüfen ---
        if equity > 0: # Nur wenn Kapital verfügbar
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
                                # *** KORRIGIERTE POSITIONSGRÖSSENBERECHNUNG (BASIEREND AUF STARTKAPITAL) ***
                                risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0) # <--- BASIERT AUF STARTKAPITAL
                                if risk_amount_usd <= 0: continue

                                sl_price = entry_price * (1 - stop_loss_pct_param)
                                sl_distance_price = abs(entry_price - sl_price)
                                if sl_distance_price <= 0: continue

                                amount_coins = risk_amount_usd / sl_distance_price
                                margin_required = (amount_coins * entry_price) / leverage

                                # *** NEUE MARGIN-PRÜFUNG ***
                                available_equity = equity - total_margin_used # Verfügbares Equity = Realisiert - Gebundene Margin
                                if margin_required > available_equity:
                                     # logger.warning(f"Sim: Insufficient AVAILABLE equity ({available_equity:.2f}) for required margin ({margin_required:.2f}) on long layer {k}. Skipping.")
                                     continue # Diesen Layer überspringen
                                # *** ENDE MARGIN-PRÜFUNG ***

                                tp_price = current_candle['average']

                                # Position hinzufügen und Margin binden
                                open_portfolio_positions[strategy_id].append({
                                    'entry_price': entry_price,
                                    'amount_coins': amount_coins,
                                    'side': side,
                                    'sl_price': sl_price,
                                    'tp_price': tp_price,
                                    'leverage': leverage
                                })
                                total_margin_used += margin_required # NEU: Margin hinzufügen

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
                                # *** KORRIGIERTE POSITIONSGRÖSSENBERECHNUNG (BASIEREND AUF STARTKAPITAL) ***
                                risk_amount_usd = start_capital * (risk_per_entry_pct / 100.0) # <--- BASIERT AUF STARTKAPITAL
                                if risk_amount_usd <= 0: continue

                                sl_price = entry_price * (1 + stop_loss_pct_param)
                                sl_distance_price = abs(entry_price - sl_price)
                                if sl_distance_price <= 0: continue

                                amount_coins = risk_amount_usd / sl_distance_price
                                margin_required = (amount_coins * entry_price) / leverage

                                # *** NEUE MARGIN-PRÜFUNG ***
                                available_equity = equity - total_margin_used
                                if margin_required > available_equity:
                                     # logger.warning(f"Sim: Insufficient AVAILABLE equity ({available_equity:.2f}) for required margin ({margin_required:.2f}) on short layer {k}. Skipping.")
                                     continue
                                # *** ENDE MARGIN-PRÜFUNG ***

                                tp_price = current_candle['average']

                                # Position hinzufügen und Margin binden
                                open_portfolio_positions[strategy_id].append({
                                    'entry_price': entry_price,
                                    'amount_coins': amount_coins,
                                    'side': side,
                                    'sl_price': sl_price,
                                    'tp_price': tp_price,
                                    'leverage': leverage
                                })
                                total_margin_used += margin_required # NEU: Margin hinzufügen

        # --- 3. Unrealisierten PnL und Equity Curve berechnen ---
        unrealized_pnl = 0.0
        # Aktuellen Wert aller offenen Positionen berechnen
        current_portfolio_value = 0.0
        for strategy_id, open_layers in open_portfolio_positions.items():
            if strategy_id not in strategy_dfs or ts not in strategy_dfs[strategy_id].index: continue
            current_price = strategy_dfs[strategy_id].loc[ts]['close'] # Aktueller Schlusskurs
            for layer in open_layers:
                leverage_layer = layer.get('leverage', 1)
                layer_amount_coins = layer['amount_coins']
                entry_price_layer = layer['entry_price']
                layer_pnl = 0
                if layer['side'] == 'long':
                    layer_pnl = (current_price - entry_price_layer) * layer_amount_coins * leverage_layer
                else: # short
                    layer_pnl = (entry_price_layer - current_price) * layer_amount_coins * leverage_layer
                unrealized_pnl += layer_pnl
                # Wert der Margin dieser Position + PnL = Aktueller Wert
                # margin_this_layer = (entry_price_layer * layer_amount_coins) / leverage_layer
                # current_portfolio_value += margin_this_layer + layer_pnl # Alternative Berechnungsmethode

        # Equity Curve basiert auf realisiertem Kapital + unrealisiertem PnL
        current_total_equity_curve = equity + unrealized_pnl
        equity_curve.append({'timestamp': ts, 'equity': current_total_equity_curve})

        # --- 4. Drawdown und Liquidation prüfen (basierend auf Equity Curve) ---
        peak_equity_curve = max(peak_equity_curve, current_total_equity_curve)
        drawdown_curve = (peak_equity_curve - current_total_equity_curve) / peak_equity_curve if peak_equity_curve > 0 else 0
        current_dd_pct_curve = drawdown_curve * 100
        if current_dd_pct_curve > max_drawdown_pct:
            max_drawdown_pct = current_dd_pct_curve
            max_drawdown_date = ts

        min_equity_during_sim = min(min_equity_during_sim, current_total_equity_curve)

        # Liquidation prüfen (wenn das *realisierte* Kapital die verwendete Margin nicht mehr deckt - vereinfacht!)
        # Eine präzisere Prüfung würde die Maintenance Margin Rate benötigen.
        # Hier prüfen wir, ob das *theoretische* Gesamt-Equity (inkl. unreal. Verluste) unter 0 fällt.
        if current_total_equity_curve <= 0 and not liquidation_date:
            liquidation_date = ts
            logger.warning(f"PORTFOLIO LIQUIDIERT (Equity <= 0) am {ts.strftime('%Y-%m-%d')}!")
            equity = 0 # Realisiertes Kapital ist weg
            # Fülle den Rest der Equity Curve mit 0
            remaining_timestamps = [t for t in simulation_timestamps if t > ts]
            for rem_ts in remaining_timestamps:
                 equity_curve.append({'timestamp': rem_ts, 'equity': 0.0})
            break # Simulation beenden

    # --- Endauswertung ---
    logger.info("3/4: Bereite Analyse-Ergebnisse vor...")
    # Finales Equity ist der letzte Wert der Equity Curve
    final_equity_curve = equity_curve[-1]['equity'] if equity_curve else start_capital
    final_equity_curve = max(0, final_equity_curve) # Sicherstellen, dass nicht negativ

    total_pnl_pct = (final_equity_curve / start_capital - 1) * 100 if start_capital > 0 else 0
    trade_count = len(closed_trades_portfolio)
    wins = sum(1 for t in closed_trades_portfolio if t['pnl'] > 0)
    win_rate = (wins / trade_count * 100) if trade_count > 0 else 0

    pnl_per_strategy_df = pd.DataFrame(closed_trades_portfolio).groupby('strategy_id')['pnl'].sum().reset_index() if closed_trades_portfolio else pd.DataFrame(columns=['strategy_id', 'pnl'])
    trades_per_strategy_df = pd.DataFrame(closed_trades_portfolio).groupby('strategy_id').size().reset_index(name='trades') if closed_trades_portfolio else pd.DataFrame(columns=['strategy_id', 'trades'])

    equity_df = pd.DataFrame(equity_curve)
    calculated_max_dd_pct = 0.0
    calculated_max_dd_date = None
    if not equity_df.empty:
        equity_df['peak'] = equity_df['equity'].cummax()
        # Drawdown als positiven Prozentwert berechnen
        equity_df['drawdown_pct'] = ((equity_df['peak'] - equity_df['equity']) / equity_df['peak'].replace(0, np.nan)).fillna(0) * 100
        # Finde den maximalen Drawdown erneut aus dem DataFrame
        if not equity_df['drawdown_pct'].empty:
             max_dd_idx = equity_df['drawdown_pct'].idxmax()
             if pd.notna(max_dd_idx): # Sicherstellen, dass der Index gültig ist
                  max_dd_row = equity_df.loc[max_dd_idx]
                  calculated_max_dd_date = max_dd_row['timestamp']
                  calculated_max_dd_pct = max_dd_row['drawdown_pct']
             else: # Fallback, wenn kein Max DD gefunden wird (z.B. alles 0)
                  calculated_max_dd_date = equity_df['timestamp'].iloc[0] if not equity_df.empty else None
                  calculated_max_dd_pct = 0.0
        else:
             calculated_max_dd_date = equity_df['timestamp'].iloc[0] if not equity_df.empty else None
             calculated_max_dd_pct = 0.0
    else: # Falls equity_df leer ist
         calculated_max_dd_date = None
         calculated_max_dd_pct = 0.0 # Oder 100.0 je nach Definition


    logger.info("4/4: Portfolio-Simulation abgeschlossen.")

    # Verwende den aus dem DataFrame berechneten Max Drawdown
    final_max_drawdown_pct = calculated_max_dd_pct
    final_max_drawdown_date = calculated_max_dd_date

    return {
        "start_capital": start_capital,
        "end_capital": final_equity_curve, # Verwende Equity-Curve-Endwert
        "total_pnl_pct": total_pnl_pct,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "max_drawdown_pct": final_max_drawdown_pct, # Korrigierter Wert
        "max_drawdown_date": final_max_drawdown_date, # Korrigierter Wert
        "min_equity": min_equity_during_sim,
        "liquidation_date": liquidation_date,
        "pnl_per_strategy": pnl_per_strategy_df,
        "trades_per_strategy": trades_per_strategy_df,
        "equity_curve": equity_df
    }
