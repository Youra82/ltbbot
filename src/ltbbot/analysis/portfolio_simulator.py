# src/ltbbot/analysis/portfolio_simulator.py
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

    Args:
        start_capital (float): Startkapital.
        strategies_data (dict): Dict mapping strategy_id to {'symbol', 'timeframe', 'data': pd.DataFrame, 'params': dict}.
        start_date (str): Startdatum JJJJ-MM-TT.
        end_date (str): Enddatum JJJJ-MM-TT.

    Returns:
        dict: Ergebnisse der Portfolio-Simulation or None if error.
    """
    logger.info("\n--- Starte Portfolio-Simulation... ---")

    if not strategies_data:
        logger.error("Keine Strategie-Daten für die Simulation übergeben.")
        return None

    # --- Daten vorbereiten: Indikatoren berechnen & globalen Zeitindex erstellen ---
    all_timestamps = set()
    strategy_dfs = {}
    processed_data_count = 0

    logger.info("1/4: Berechne Indikatoren für alle Strategien...")
    for strategy_id, strat_info in strategies_data.items():
        try:
            # Berechne Indikatoren für die Rohdaten
            df_with_indicators, _ = calculate_indicators_and_signals(strat_info['data'].copy(), strat_info['params'])
            if df_with_indicators.empty:
                logger.warning(f"Keine Indikatordaten für Strategie {strategy_id}. Wird ignoriert.")
                continue

            strategy_dfs[strategy_id] = df_with_indicators # Speichere DataFrame mit Indikatoren
            all_timestamps.update(df_with_indicators.index)
            processed_data_count += 1
        except Exception as e:
            logger.error(f"Fehler bei Indikatorberechnung für {strategy_id}: {e}. Wird ignoriert.")
            continue

    if processed_data_count == 0:
         logger.error("Konnte für keine Strategie Indikatoren berechnen. Breche Simulation ab.")
         return None

    # Sortierte Liste aller eindeutigen Zeitstempel über alle Strategien
    sorted_timestamps = sorted(list(all_timestamps))
    # Filtere Zeitstempel auf den gewünschten Simulationszeitraum
    sim_start_ts = pd.to_datetime(start_date + " 00:00:00+00:00", utc=True)
    sim_end_ts = pd.to_datetime(end_date + " 23:59:59+00:00", utc=True)
    simulation_timestamps = [ts for ts in sorted_timestamps if sim_start_ts <= ts <= sim_end_ts]

    if not simulation_timestamps:
        logger.error("Keine gültigen Zeitstempel im Simulationszeitraum gefunden.")
        return None

    logger.info(f"Zeitraum: {simulation_timestamps[0]} bis {simulation_timestamps[-1]}")
    logger.info("2/4: Starte chronologische Simulation...")

    # --- Simulationsvariablen initialisieren ---
    equity = start_capital
    peak_equity = start_capital
    min_equity_during_sim = start_capital
    max_drawdown_pct = 0.0
    max_drawdown_date = None
    liquidation_date = None

    # Portfolio-Positionen: {strategy_id: [list of open layers for that strategy]}
    open_portfolio_positions = {strategy_id: [] for strategy_id in strategy_dfs.keys()}
    closed_trades_portfolio = [] # Liste aller geschlossenen Trades über alle Strategien
    equity_curve = [] # Liste von Dicts {'timestamp': ts, 'equity': equity_value}

    fee_pct = 0.0006 # Annahme für Gebühren

    # --- Simulations-Loop ---
    for ts in tqdm(simulation_timestamps, desc="Simuliere Portfolio"):
        if liquidation_date: # Abbrechen, wenn liquidiert
            break

        current_equity_snapshot = equity # Eigenkapital zu Beginn des Zeitstempels
        total_exit_pnl_this_step = 0.0

        # --- 1. Ausstiege (SL & TP) für alle offenen Positionen prüfen ---
        for strategy_id, open_layers in open_portfolio_positions.items():
            if strategy_id not in strategy_dfs: continue # Überspringe, falls Indikatoren fehlten
            strat_df = strategy_dfs[strategy_id]

            # Prüfe, ob Daten für diesen Zeitstempel vorhanden sind
            if ts not in strat_df.index:
                continue # Keine Kerze für diese Strategie zu diesem Zeitpunkt

            current_candle = strat_df.loc[ts]
            remaining_layers = [] # Layers, die nach diesem Schritt offen bleiben

            for layer in open_layers:
                exited = False
                exit_price = None
                pnl = 0.0
                leverage = layer.get('leverage', 1) # Hebel aus Layer holen

                # SL Prüfung
                if layer['side'] == 'long' and current_candle['low'] <= layer['sl_price']:
                    exit_price = layer['sl_price']
                    exited = True
                elif layer['side'] == 'short' and current_candle['high'] >= layer['sl_price']:
                    exit_price = layer['sl_price']
                    exited = True

                # TP Prüfung (nur wenn SL nicht getroffen)
                if not exited:
                     # TP ist der Average *dieser* Kerze
                    tp_price_current = current_candle['average']
                    if layer['side'] == 'long' and current_candle['high'] >= tp_price_current:
                        if current_candle['open'] >= tp_price_current or current_candle['low'] <= tp_price_current:
                             exit_price = tp_price_current
                             exited = True
                    elif layer['side'] == 'short' and current_candle['low'] <= tp_price_current:
                         if current_candle['open'] <= tp_price_current or current_candle['high'] >= tp_price_current:
                             exit_price = tp_price_current
                             exited = True

                # Wenn Ausstieg, PnL berechnen und Layer entfernen
                if exited and exit_price is not None:
                    if layer['side'] == 'long':
                        pnl = (exit_price - layer['entry_price']) * layer['amount'] * leverage
                    else: # short
                        pnl = (layer['entry_price'] - exit_price) * layer['amount'] * leverage

                    # Gebühren
                    entry_value = layer['entry_price'] * layer['amount'] * leverage
                    exit_value = exit_price * layer['amount'] * leverage
                    fees = (entry_value * fee_pct) + (exit_value * fee_pct)
                    pnl -= fees

                    total_exit_pnl_this_step += pnl
                    closed_trades_portfolio.append({'pnl': pnl, 'side': layer['side'], 'strategy_id': strategy_id})
                    # Layer nicht in remaining_layers aufnehmen
                else:
                    remaining_layers.append(layer) # Layer bleibt offen

            open_portfolio_positions[strategy_id] = remaining_layers # Update offene Layer für diese Strategie

        equity += total_exit_pnl_this_step # Equity nach allen Ausstiegen aktualisieren

        # --- 2. Einstiege für alle Strategien prüfen ---
        # Annahme: Jede Strategie handelt unabhängig, teilt sich aber das Gesamtkapital
        # Wir berechnen die Größe basierend auf dem aktuellen Equity *vor* neuen Einstiegen
        balance_fraction_total = 1.0 # Annahme: Portfolio nutzt 100% des Kapitals (anpassbar)
        available_capital_for_new_entries = equity * balance_fraction_total

        # Zähle, wie viele Strategien potenziell handeln könnten (hat Daten für ts)
        active_strategies_count = sum(1 for sid in strategy_dfs if ts in strategy_dfs[sid].index)
        if active_strategies_count == 0: active_strategies_count = 1 # Div/0 verhindern

        # Kapital pro Strategie (vereinfacht, gleichmäßig aufgeteilt)
        capital_per_strategy = available_capital_for_new_entries / active_strategies_count

        for strategy_id, strat_df in strategy_dfs.items():
             if ts not in strat_df.index:
                 continue # Keine Daten für diese Strategie

             current_candle = strat_df.loc[ts]
             params = strategies_data[strategy_id]['params'] # Hole Parameter für diese Strategie
             strategy_params = params['strategy']
             risk_params = params['risk']
             behavior_params = params['behavior']
             leverage = risk_params['leverage']
             num_envelopes = len(strategy_params['envelopes'])
             stop_loss_pct = risk_params['stop_loss_pct'] / 100.0

             # Kapital pro Order für diese Strategie
             capital_per_order = (capital_per_strategy / num_envelopes) if num_envelopes > 0 else 0

             # Long Entries
             if behavior_params.get('use_longs', True) and capital_per_order > 0:
                 for k in range(1, num_envelopes + 1):
                     low_band_col = f'band_low_{k}'
                     if low_band_col not in current_candle: continue
                     entry_trigger_price = current_candle[low_band_col]

                     if current_candle['low'] <= entry_trigger_price:
                         entry_price = entry_trigger_price # Vereinfacht: Einstieg zum Trigger
                         if entry_price > 0:
                             amount = capital_per_order / entry_price # Menge in Coins
                             sl_price = entry_price * (1 - stop_loss_pct)
                             tp_price = current_candle['average'] # TP ist dynamisch

                             open_portfolio_positions[strategy_id].append({
                                 'entry_price': entry_price, 'amount': amount, 'side': 'long',
                                 'sl_price': sl_price, 'tp_price': tp_price, 'leverage': leverage
                             })

             # Short Entries
             if behavior_params.get('use_shorts', True) and capital_per_order > 0:
                  for k in range(1, num_envelopes + 1):
                      high_band_col = f'band_high_{k}'
                      if high_band_col not in current_candle: continue
                      entry_trigger_price = current_candle[high_band_col]

                      if current_candle['high'] >= entry_trigger_price:
                          entry_price = entry_trigger_price
                          if entry_price > 0:
                              amount = capital_per_order / entry_price
                              sl_price = entry_price * (1 + stop_loss_pct)
                              tp_price = current_candle['average']

                              open_portfolio_positions[strategy_id].append({
                                 'entry_price': entry_price, 'amount': amount, 'side': 'short',
                                 'sl_price': sl_price, 'tp_price': tp_price, 'leverage': leverage
                             })

        # --- 3. Unrealisierten PnL und Equity Curve berechnen ---
        unrealized_pnl = 0.0
        for strategy_id, open_layers in open_portfolio_positions.items():
            if strategy_id not in strategy_dfs or ts not in strategy_dfs[strategy_id].index: continue
            current_price = strategy_dfs[strategy_id].loc[ts]['close'] # Aktueller Schlusskurs
            for layer in open_layers:
                leverage = layer.get('leverage', 1)
                if layer['side'] == 'long':
                    unrealized_pnl += (current_price - layer['entry_price']) * layer['amount'] * leverage
                else: # short
                    unrealized_pnl += (layer['entry_price'] - current_price) * layer['amount'] * leverage

        current_total_equity = equity + unrealized_pnl # Realisiertes Kapital + Unrealisierter PnL
        equity_curve.append({'timestamp': ts, 'equity': current_total_equity})

        # --- 4. Drawdown und Liquidation prüfen ---
        peak_equity = max(peak_equity, current_total_equity)
        drawdown = (peak_equity - current_total_equity) / peak_equity if peak_equity > 0 else 0
        if drawdown * 100 > max_drawdown_pct: # Drawdown in % speichern
             max_drawdown_pct = drawdown * 100
             max_drawdown_date = ts

        min_equity_during_sim = min(min_equity_during_sim, current_total_equity)

        if current_total_equity <= 0 and not liquidation_date:
            liquidation_date = ts
            logger.warning(f"PORTFOLIO LIQUIDIERT am {ts.strftime('%Y-%m-%d')}!")
            # Optional: Setze Equity auf 0 für den Rest der Kurve
            # equity = 0 # Nur realisiertes Kapital
            # continue # Nächsten Timestamp prüfen? Oder abbrechen? Besser abbrechen.
            break # Simulation beenden

    logger.info("3/4: Bereite Analyse-Ergebnisse vor...")
    final_equity = equity_curve[-1]['equity'] if equity_curve else start_capital
    total_pnl_pct = (final_equity / start_capital - 1) * 100 if start_capital > 0 else 0
    trade_count = len(closed_trades_portfolio)
    wins = sum(1 for t in closed_trades_portfolio if t['pnl'] > 0)
    win_rate = (wins / trade_count * 100) if trade_count > 0 else 0

    # PnL pro Strategie (optional)
    pnl_per_strategy_df = pd.DataFrame(closed_trades_portfolio).groupby('strategy_id')['pnl'].sum().reset_index()
    trades_per_strategy_df = pd.DataFrame(closed_trades_portfolio).groupby('strategy_id').size().reset_index(name='trades')

    # Equity Curve DataFrame erstellen und Drawdown berechnen
    equity_df = pd.DataFrame(equity_curve)
    if not equity_df.empty:
        equity_df['peak'] = equity_df['equity'].cummax()
        # Drawdown als positiven Prozentwert berechnen
        equity_df['drawdown_pct'] = ((equity_df['peak'] - equity_df['equity']) / equity_df['peak'].replace(0, np.nan)).fillna(0) * 100
        # Finde das Datum des maximalen Drawdowns erneut aus dem DataFrame
        max_drawdown_row = equity_df.loc[equity_df['drawdown_pct'].idxmax()] if not equity_df.empty else None
        max_drawdown_date = max_drawdown_row['timestamp'] if max_drawdown_row is not None else None
        max_drawdown_pct = equity_df['drawdown_pct'].max() if not equity_df.empty else 100.0


    logger.info("4/4: Portfolio-Simulation abgeschlossen.")

    return {
        "start_capital": start_capital,
        "end_capital": final_equity,
        "total_pnl_pct": total_pnl_pct,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "max_drawdown_pct": max_drawdown_pct,
        "max_drawdown_date": max_drawdown_date,
        "min_equity": min_equity_during_sim,
        "liquidation_date": liquidation_date,
        "pnl_per_strategy": pnl_per_strategy_df,
        "trades_per_strategy": trades_per_strategy_df,
        "equity_curve": equity_df # <-- WICHTIG: DataFrame zurückgeben
    }
