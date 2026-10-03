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
from ltbbot.strategy.envelope_logic import (calculate_indicators_and_signals, calculate_position_margin, margin_fits,
                                            classify_regime, compute_band_sl_price, simulate_entry_fill,
                                            stop_fill_price)
from ltbbot.analysis.backtester import _resolve_ambiguous_exit, _get_fine_slice

# --- KONSTANTEN FÜR REALISTISCHERE SIMULATION ---
SLIPPAGE_PCT_EXIT  = 0.0005  # 0.05% Slippage auf Exit (Market Order TP/SL)
SLIPPAGE_PCT_ENTRY = 0.0012  # 0.12% Slippage auf Entry (Trigger-Limit, wie backtester.py)
# --- ENDE KONSTANTEN ---


def run_portfolio_simulation(start_capital, strategies_data, start_date, end_date, multi_band_entries=True):
    """
    Führt eine chronologische Portfolio-Simulation mit mehreren Envelope-Strategien durch.
    EINHEITLICHE LOGIK mit backtester.py (2026-08-27 nachgezogen, siehe dortiger
    multi_band_entries-Docstring): Standard (multi_band_entries=False) bleibt wie
    bisher Band-1-only + statisches Kapital, damit alte Aufrufer/Vergleiche
    unveraendert bleiben. multi_band_entries=True + laufendes `equity` als
    Risikobasis bilden das echte Live-Verhalten ab (alle Baender gleichzeitig,
    Compounding) -- fuer den Portfolio-Optimizer (--auto-write) jetzt Standard.
    - SL 1.5x breiter im TREND (ADX 25-30)
    - Trend-Bias: Im Uptrend nur Longs, im Downtrend nur Shorts
    - Kein Trading bei STRONG_TREND (ADX > 30)
    - Kein Lookahead (2026-09-27): Baender/TP/Regime aus der letzten abgeschlossenen
      Kerze, Fills per simulate_entry_fill() -- identisch zu backtester.py
    """
    _band_desc = "alle Baender gleichzeitig" if multi_band_entries else "max. 1 Pos/Strategie (Band-1-only)"
    logger.info(f"\n--- Starte Portfolio-Simulation (Live-Bot-Logik, {_band_desc})... ---")

    if not strategies_data:
        logger.error("Keine Strategie-Daten für die Simulation übergeben.")
        return None

    # --- Daten vorbereiten ---
    all_timestamps = set()
    strategy_dfs = {}
    strategy_fine_data = {}
    strategy_coarse_duration = {}
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
            strategy_fine_data[strategy_id] = strat_info.get('fine_data')
            strategy_coarse_duration[strategy_id] = (
                df_with_indicators.index[1] - df_with_indicators.index[0]
                if len(df_with_indicators.index) >= 2 else None
            )
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
        _risk = strategies_data[strategy_id]['params']['risk']
        _atr_sl = None
        if 'sl_to_env1_ratio' not in _risk and 'stop_loss_atr_multiplier' in _risk:
            _atr_sl = _ta.volatility.average_true_range(df['high'], df['low'], df['close'],
                                                        window=_risk.get('stop_loss_atr_period', 14))
        strategy_pre_indicators[strategy_id] = {
            'adx':    _ta.trend.adx(df['high'], df['low'], df['close'], window=14),
            'sma20':  _ta.trend.sma_indicator(df['close'], window=20),
            'sma50':  _ta.trend.sma_indicator(df['close'], window=50),
            'atr_sl': _atr_sl,
            'atr_pct': _ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14) / df['close'],
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

    # Margin ist ueber ALLE Strategien/Symbole hinweg eine gemeinsame, endliche
    # Ressource (ein Bitget-Konto, kein Konto pro Strategie). used_margin trackt die
    # Summe ueber alle aktuell offenen Layer (jede Strategie, jedes Band); vor jeder
    # neuen Order wird geprueft ob genug FREIE Margin (equity - used_margin) uebrig
    # ist -- sonst wuerde die Order live mit InsufficientFunds abgelehnt (User-Vorgabe
    # 2026-09-02: ein Trade der schon (fast) das ganze Kapital bindet darf einen
    # zweiten, gleichzeitigen -- egal ob anderes Band oder anderes Symbol -- NICHT
    # zulassen).
    used_margin = 0.0

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
                pos_amount_pnl = layer_pnl_calc['amount_coins']
                pos_entry_pnl  = layer_pnl_calc['entry_price']
                if layer_pnl_calc['side'] == 'long':
                    unrealized_pnl_start += (current_price_for_pnl - pos_entry_pnl) * pos_amount_pnl
                else:
                    unrealized_pnl_start += (pos_entry_pnl - current_price_for_pnl) * pos_amount_pnl

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
        # KEIN LOOKAHEAD (2026-09-27, identisch zu backtester.py): waehrend Kerze ts
        # gelten TP-MA/Baender/Regime der letzten ABGESCHLOSSENEN Kerze (Index-1).
        total_exit_pnl_this_step = 0.0
        closed_bands_this_step = {strategy_id: set() for strategy_id in strategy_dfs.keys()}

        def _close_layer(strategy_id, layer, exit_price, exit_reason, atr_pct=None):
            pos_side = layer['side']; pos_entry = layer['entry_price']; pos_amount = layer['amount_coins']
            if exit_reason == 'SL':
                exit_price = stop_fill_price(pos_side, exit_price, atr_pct)
            if pos_side == 'long':
                pnl = (exit_price - pos_entry) * pos_amount
            else:
                pnl = (pos_entry - exit_price) * pos_amount
            entry_notional = pos_entry * pos_amount
            exit_notional  = exit_price * pos_amount
            pnl -= (entry_notional * fee_pct) + (exit_notional * fee_pct)
            pnl -= abs(exit_notional  * SLIPPAGE_PCT_EXIT)
            pnl -= abs(entry_notional * SLIPPAGE_PCT_ENTRY)
            pnl_pct = (pnl / entry_notional) * 100 if entry_notional > 0 else 0.0
            closed_trades_portfolio.append({
                'exit_time':    ts,
                'entry_time':   layer.get('entry_time', ts),
                'symbol':       strategies_data[strategy_id]['symbol'],
                'timeframe':    strategies_data[strategy_id]['timeframe'],
                'side':         pos_side,
                'band':         layer.get('band'),
                'entry_price':  round(pos_entry, 6),
                'exit_price':   round(exit_price, 6),
                'sl_price':     round(layer['sl_price'], 6),
                'leverage':     layer.get('leverage', 1),
                'amount_coins': round(pos_amount, 8),
                'pnl_usd':      round(pnl, 4),
                'pnl_pct':      round(pnl_pct, 2),
                'reason':       'WIN' if pnl > 0 else 'SL',
                'exit_reason':  exit_reason,
                'strategy_id':  strategy_id,
            })
            return pnl

        for strategy_id, open_layers in open_portfolio_positions.items():
            if not open_layers or strategy_id not in strategy_dfs or ts not in strategy_dfs[strategy_id].index:
                continue
            strat_df = strategy_dfs[strategy_id]
            df_idx = strat_df.index.get_loc(ts)
            if df_idx < 1:
                continue
            current_candle = strat_df.iloc[df_idx]
            signal_candle = strat_df.iloc[df_idx - 1]
            c_open, c_high, c_low = current_candle['open'], current_candle['high'], current_candle['low']
            tp_price_current = signal_candle['average']
            tp_valid = pd.notna(tp_price_current) and tp_price_current > 0
            remaining_layers = []

            for layer in open_layers:
                pos_side = layer['side']
                pos_sl   = layer['sl_price']
                exit_price = None; exit_reason = None
                if pos_side == 'long':
                    sl_hit = c_low <= pos_sl
                    tp_hit = tp_valid and c_high >= tp_price_current
                    sl_gap = c_open <= pos_sl
                    tp_gap = tp_valid and c_open >= tp_price_current
                else:
                    sl_hit = c_high >= pos_sl
                    tp_hit = tp_valid and c_low <= tp_price_current
                    sl_gap = c_open >= pos_sl
                    tp_gap = tp_valid and c_open <= tp_price_current

                if sl_gap:
                    exit_price, exit_reason = c_open, 'SL'
                elif tp_gap:
                    exit_price, exit_reason = c_open, 'TP'
                elif sl_hit and tp_hit:
                    # Beide Level in derselben Kerze moeglich -- per Fein-Daten
                    # (falls vorhanden) real aufloesen statt SL zu bevorzugen
                    # (oraclebot-Muster).
                    resolved = None
                    fine_data = strategy_fine_data.get(strategy_id)
                    coarse_duration = strategy_coarse_duration.get(strategy_id)
                    if fine_data is not None and coarse_duration is not None:
                        fine_slice = _get_fine_slice(fine_data, ts, ts + coarse_duration)
                        resolved = _resolve_ambiguous_exit(fine_slice, pos_sl, tp_price_current, pos_side)
                    if resolved is None or resolved == pos_sl:
                        exit_price, exit_reason = pos_sl, 'SL'  # Fallback: SL-first-Konvention
                    else:
                        exit_price, exit_reason = tp_price_current, 'TP'
                elif sl_hit:
                    exit_price, exit_reason = pos_sl, 'SL'
                elif tp_hit:
                    exit_price, exit_reason = tp_price_current, 'TP'

                if exit_price is not None and exit_price > 0:
                    total_exit_pnl_this_step += _close_layer(strategy_id, layer, exit_price, exit_reason,
                                                             strategy_pre_indicators[strategy_id]['atr_pct'].iloc[df_idx - 1])
                    used_margin -= layer.get('margin', 0.0) # Margin wieder freigeben
                    closed_bands_this_step[strategy_id].add((pos_side, layer.get('band')))
                else:
                    remaining_layers.append(layer)

            open_portfolio_positions[strategy_id] = remaining_layers

        used_margin = max(0.0, used_margin) # Rundungsdrift abfangen
        equity += total_exit_pnl_this_step

        # --- Einstiege prüfen ---
        # Wie Live Bot: bei offener Position nur weitere, noch nicht offene Baender
        # auf DERSELBEN Seite (Bitget One-Way-Modus, identisch zu backtester.py)
        if equity > 0:
            for strategy_id, strat_df in strategy_dfs.items():
                if ts not in strat_df.index:
                    continue
                df_idx = strat_df.index.get_loc(ts)
                if df_idx < 1:
                    continue

                open_layers = open_portfolio_positions[strategy_id]
                open_side = open_layers[0]['side'] if open_layers else None
                open_bands = {(l['side'], l.get('band')) for l in open_layers}

                current_candle = strat_df.iloc[df_idx]
                signal_candle  = strat_df.iloc[df_idx - 1]  # letzte abgeschlossene Kerze
                params         = strategies_data[strategy_id]['params']
                strategy_params = params['strategy']
                risk_params     = params['risk']
                behavior_params = params['behavior']
                leverage            = risk_params['leverage']
                num_envelopes       = len(strategy_params['envelopes'])
                risk_per_entry_pct  = risk_params.get('risk_per_entry_pct', 0.5)
                use_longs  = behavior_params.get('use_longs', True)
                use_shorts = behavior_params.get('use_shorts', True)

                # Marktregime der letzten abgeschlossenen Kerze (geteilte Funktion mit Live)
                pre = strategy_pre_indicators[strategy_id]
                if df_idx - 1 >= 49:
                    regime, trade_allowed, trend_direction = classify_regime(
                        pre['adx'].iloc[df_idx - 1], signal_candle['close'], signal_candle['average'],
                        pre['sma20'].iloc[df_idx - 1], pre['sma50'].iloc[df_idx - 1], strategy_params)
                else:
                    regime, trade_allowed, trend_direction = "UNCERTAIN", True, "NEUTRAL"

                # STRONG_TREND: kein Einstieg (wie Live Bot)
                if not trade_allowed:
                    continue

                # Trend-Bias (wie Live Bot)
                current_use_longs  = use_longs and trend_direction != "DOWNTREND"
                current_use_shorts = use_shorts and trend_direction != "UPTREND"

                # Risiko basiert auf dem AKTUELL FREIEN Portfolio-Kapital, nicht auf
                # dem vollen `equity` (2026-09-04 korrigiert): Live nutzt fuer
                # risk_base_capital den tatsaechlich freien Kontostand
                # (fetch_balance_usdt() -> Bitgets 'free', bereits abzueglich der
                # Margin aller anderen offenen Positionen -- ein Konto, geteilte
                # Margin ueber alle Strategien).
                available_capital = max(0.0, equity - used_margin)
                risk_amount_usd = available_capital * (risk_per_entry_pct / 100.0)
                if risk_amount_usd <= 0:
                    continue

                trigger_delta_pct = strategy_params.get('trigger_price_delta_pct', 0.05) / 100.0
                atr_value = pre['atr_sl'].iloc[df_idx - 1] if pre['atr_sl'] is not None else None
                MIN_NOTIONAL_USDT = 5.0
                c_open = current_candle['open']
                candidates = {'long': [], 'short': []}
                _fine_memo = {}

                def _entry_fine_bars(_sid=strategy_id, _ts=ts, _memo=_fine_memo):
                    # Fein-Kerzen dieser Entry-Kerze, nur bei Bedarf geladen (einmal je Kerze)
                    if 'v' not in _memo:
                        _fd = strategy_fine_data.get(_sid)
                        _cd = strategy_coarse_duration.get(_sid)
                        _memo['v'] = _get_fine_slice(_fd, _ts, _ts + _cd) if _fd is not None and _cd is not None else None
                    return _memo['v']
                for side, allowed in (('long', current_use_longs), ('short', current_use_shorts)):
                    if not allowed:
                        continue
                    if open_side is not None and side != open_side:
                        continue
                    for k in range(1, num_envelopes + 1):
                        if (side, k) in open_bands or (side, k) in closed_bands_this_step[strategy_id]:
                            continue
                        band_col = f'band_low_{k}' if side == 'long' else f'band_high_{k}'
                        band_price = signal_candle.get(band_col, float('nan'))
                        if pd.isna(band_price) or band_price <= 0 or pd.isna(signal_candle['close']):
                            continue
                        # Close-Confirmation: letzte abgeschlossene Kerze muss jenseits des Bands geschlossen haben
                        if side == 'long' and signal_candle['close'] > band_price:
                            continue
                        if side == 'short' and signal_candle['close'] < band_price:
                            continue
                        sl_price = compute_band_sl_price(side, band_price, k - 1, params, regime, atr_value)
                        if sl_price is None:
                            continue
                        sl_dist = abs(band_price - sl_price)
                        if sl_dist <= 0:
                            continue
                        trigger_price = band_price * (1 - trigger_delta_pct) if side == 'long' else band_price * (1 + trigger_delta_pct)
                        fill_price, stopped = simulate_entry_fill(side, c_open, current_candle['high'], current_candle['low'],
                                                                  current_candle['close'], trigger_price, sl_price,
                                                                  fine_bars=_entry_fine_bars)
                        if fill_price is None:
                            continue
                        amount_coins = risk_amount_usd / sl_dist
                        if amount_coins * band_price < MIN_NOTIONAL_USDT:
                            continue
                        candidates[side].append((fill_price, sl_price, amount_coins,
                                                 abs(c_open - trigger_price), k, stopped))
                        if not multi_band_entries:
                            break

                # Wenn beide Seiten gleichzeitig: naeherer Trigger zum Open gewinnt
                if candidates['long'] and candidates['short']:
                    nearest_long = min(c[3] for c in candidates['long'])
                    nearest_short = min(c[3] for c in candidates['short'])
                    if nearest_long <= nearest_short:
                        candidates['short'] = []
                    else:
                        candidates['long'] = []

                # Reihenfolge Band 1->3 = engste (naeheste) Baender zuerst. Jede Order
                # muss sich gegen die noch FREIE Margin behaupten -- ueber ALLE
                # Strategien/Symbole hinweg (ein Bitget-Konto). Reicht sie nicht mehr,
                # wird die Order uebersprungen (= live InsufficientFunds).
                for side in ('long', 'short'):
                    for ep, sl, amt, _, band_k, stopped in candidates[side]:
                        margin_required = calculate_position_margin(amt, ep, leverage)
                        if not margin_fits(used_margin, margin_required, equity):
                            continue
                        layer = {
                            'entry_price': ep, 'amount_coins': amt, 'side': side,
                            'sl_price': sl, 'leverage': leverage, 'entry_time': ts,
                            'margin': margin_required, 'band': band_k,
                        }
                        if stopped:
                            # SL noch in der Entry-Kerze erreicht (siehe simulate_entry_fill)
                            equity += _close_layer(strategy_id, layer, sl, 'SL',
                                                   pre['atr_pct'].iloc[df_idx - 1])
                            continue
                        used_margin += margin_required
                        open_portfolio_positions[strategy_id].append(layer)

    # --- Endauswertung ---
    logger.info("3/4: Bereite Analyse-Ergebnisse vor...")
    final_equity_curve_val = equity_curve[-1]['equity'] if equity_curve else start_capital
    final_equity_curve_val = max(0, final_equity_curve_val)

    total_pnl_pct = (final_equity_curve_val / start_capital - 1) * 100 if start_capital > 0 else 0
    trade_count = len(closed_trades_portfolio)
    wins = sum(1 for t in closed_trades_portfolio if t['pnl_usd'] > 0)
    win_rate = (wins / trade_count * 100) if trade_count > 0 else 0

    trades_df = pd.DataFrame(closed_trades_portfolio) if closed_trades_portfolio else pd.DataFrame(
        columns=['exit_time','entry_time','symbol','timeframe','side','band','entry_price','exit_price','sl_price','leverage','amount_coins','pnl_usd','pnl_pct','reason','exit_reason','strategy_id'])

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
