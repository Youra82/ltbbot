# src/ltbbot/analysis/portfolio_optimizer.py
import pandas as pd
import itertools
from tqdm import tqdm
import logging
import os
import sys
import json # Added for saving results

# Verwende den Logger, der in show_results.py konfiguriert wird
logger = logging.getLogger("show_results") # Verwende denselben Logger-Namen

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# Import the portfolio simulator which internally uses the envelope backtester
from ltbbot.analysis.portfolio_simulator import run_portfolio_simulation

# --- Portfolio Optimizer Logic (Greedy Approach) ---
def run_portfolio_optimizer(start_capital, strategies_data, start_date, end_date, max_portfolio_dd_constraint=0.3): # NEU: max_portfolio_dd_constraint hinzugefügt
    """
    Findet eine gute Kombination von Envelope-Strategien mithilfe eines Greedy-Algorithmus.
    Optimierungsziel: Maximierung einer risikoadjustierten Metrik (vereinfachte Calmar Ratio).
    Stellt sicher, dass jedes Symbol nur einmal im Portfolio vorkommt und der Portfolio-Drawdown eingehalten wird.
    """
    logger.info("\n--- Starte automatische Portfolio-Optimierung (Envelope, Symbol-Exklusiv, Portfolio DD Constraint)... ---")

    if not strategies_data:
        logger.error("Keine Strategien zum Optimieren übergeben.")
        return None

    # --- 1. Einzel-Performance analysieren (nur profitable, nicht liquidierte Strategien betrachten) ---
    logger.info("1/3: Analysiere Einzel-Performance jeder Strategie...")
    single_strategy_results = []

    for strategy_id, strat_data in tqdm(strategies_data.items(), desc="Bewerte Einzelstrategien"):
        sim_data_single = {strategy_id: strat_data}
        try:
            result = run_portfolio_simulation(start_capital, sim_data_single, start_date, end_date)

            if result and result.get("end_capital", 0) > 0 and not result.get("liquidation_date"):
                pnl_pct = result.get('total_pnl_pct', -100.0)
                max_dd_pct = result.get('max_drawdown_pct', 100.0)
                if max_dd_pct < 0.1: max_dd_pct = 0.1 # Minimalwert für Score

                # Score = Vereinfachte Calmar Ratio (PnL / Max Drawdown)
                score = pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct if pnl_pct > 0 else -1000

                # Nur Strategien berücksichtigen, deren EIGENER Drawdown unter dem Portfolio-Limit liegt
                # (Optional, aber sinnvoll, um extrem riskante Einzelstrategien auszuschließen)
                if max_dd_pct <= max_portfolio_dd_constraint * 100:
                    single_strategy_results.append({
                        'strategy_id': strategy_id,
                        'symbol': strat_data['symbol'], # Symbol für spätere Prüfung
                        'score': score,
                        'pnl_pct': pnl_pct,
                        'max_dd_pct': max_dd_pct,
                        'result': result # Vollständiges Ergebnis speichern
                    })
                else:
                     logger.debug(f"Strategie {strategy_id} ausgeschlossen: Einzel-DD ({max_dd_pct:.1f}%) > Portfolio-Limit ({max_portfolio_dd_constraint*100:.1f}%).")

            else:
                reason = "Liquidiert" if result and result.get("liquidation_date") else "Nicht profitabel/Fehler"
                logger.debug(f"Strategie {strategy_id} ausgeschlossen: {reason}")

        except Exception as e:
            logger.error(f"Fehler bei Einzel-Simulation von {strategy_id}: {e}", exc_info=False)
            continue

    if not single_strategy_results:
        logger.error(f"Keine einzige Strategie war profitabel, überlebensfähig und unter dem Portfolio DD Limit ({max_portfolio_dd_constraint*100:.1f}%). Portfolio-Optimierung nicht möglich.")
        return None

    # --- 2. Greedy-Algorithmus: Bestes Team aufbauen ---
    single_strategy_results.sort(key=lambda x: x['score'], reverse=True)

    best_portfolio_ids = [single_strategy_results[0]['strategy_id']]
    best_portfolio_symbols = {single_strategy_results[0]['symbol']} # Set mit Symbolen
    best_portfolio_score = single_strategy_results[0]['score']
    best_portfolio_result = single_strategy_results[0]['result']

    logger.info(f"2/3: Star-Spieler gefunden: {best_portfolio_ids[0]} (Symbol: {single_strategy_results[0]['symbol']}, Score: {best_portfolio_score:.2f})")
    logger.info("3/3: Suche die besten Team-Kollegen...")

    candidate_pool = single_strategy_results[1:] # Pool der verbleibenden Kandidaten (als Dictionaries)

    while True:
        best_next_addition_candidate = None
        best_score_with_addition = best_portfolio_score
        current_best_result_with_addition = best_portfolio_result

        candidates_to_evaluate = list(candidate_pool)
        progress_bar = tqdm(candidates_to_evaluate, desc=f"Teste Team mit {len(best_portfolio_ids)+1} Mitgliedern", leave=False)

        for candidate in progress_bar:
            candidate_id = candidate['strategy_id']
            candidate_symbol = candidate['symbol']

            # --- Symbol-Exklusivitäts-Check ---
            if candidate_symbol in best_portfolio_symbols:
                continue # Nächsten Kandidaten prüfen

            # Potenzielle neue Team-Zusammensetzung
            current_potential_team_ids = best_portfolio_ids + [candidate_id]
            current_team_data = {
                team_member_id: strategies_data[team_member_id]
                for team_member_id in current_potential_team_ids
            }

            # --- Simulation für das potenzielle Team ---
            try:
                result = run_portfolio_simulation(start_capital, current_team_data, start_date, end_date)

                # Bewerte das Ergebnis
                if result and result.get("end_capital", 0) > 0 and not result.get("liquidation_date"):
                    pnl_pct = result.get('total_pnl_pct', -100.0)
                    max_dd_pct = result.get('max_drawdown_pct', 100.0)
                    if max_dd_pct < 0.1: max_dd_pct = 0.1

                    # *** NEU: Portfolio DD Constraint Check ***
                    if max_dd_pct > max_portfolio_dd_constraint * 100:
                         # logger.debug(f"Potenzielles Team mit {candidate_id} überschreitet Portfolio DD Limit ({max_dd_pct:.1f}% > {max_portfolio_dd_constraint*100:.1f}%).")
                         continue # Dieses Team ist ungültig

                    score = pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct if pnl_pct > 0 else -1000

                    # Wenn dieser Kandidat das Team verbessert (und DD einhält), merke ihn dir
                    if score > best_score_with_addition:
                        best_score_with_addition = score
                        best_next_addition_candidate = candidate
                        current_best_result_with_addition = result

            except Exception as e:
                logger.error(f"Fehler bei Simulation von Team mit {candidate_id}: {e}", exc_info=False)
                continue

        # --- Entscheide, ob das Team erweitert wird ---
        if best_next_addition_candidate:
            best_next_id = best_next_addition_candidate['strategy_id']
            best_next_symbol = best_next_addition_candidate['symbol']

            logger.info(f"-> Füge hinzu: {best_next_id} (Symbol: {best_next_symbol}, Neuer Score: {best_score_with_addition:.2f})")
            best_portfolio_ids.append(best_next_id)
            best_portfolio_symbols.add(best_next_symbol) # Symbol zum Set hinzufügen
            best_portfolio_score = best_score_with_addition
            best_portfolio_result = current_best_result_with_addition

            # Entferne hinzugefügten Kandidaten aus Pool
            candidate_pool = [c for c in candidate_pool if c['strategy_id'] != best_next_id]

            if not candidate_pool:
                logger.info("Alle Kandidaten getestet.")
                break
        else:
            logger.info("Keine weitere Verbesserung durch Hinzufügen von Strategien gefunden (unter Berücksichtigung der Symbol-Exklusivität und DD-Constraint). Optimierung beendet.")
            break

    # --- Endergebnis zurückgeben ---
    logger.info(f"Optimierung abgeschlossen. Bestes Portfolio hat {len(best_portfolio_ids)} Strategien mit Score {best_portfolio_score:.2f}.")

    # Speichere das Ergebnis optional als JSON
    results_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
    os.makedirs(results_dir, exist_ok=True)
    results_file_path = os.path.join(results_dir, 'portfolio_optimization_results.json')
    try:
        # Erstelle eine serialisierbare Kopie des Ergebnisses
        final_result_serializable = best_portfolio_result.copy()

        # Serialisiere DataFrames und Timestamps
        if 'equity_curve' in final_result_serializable and isinstance(final_result_serializable['equity_curve'], pd.DataFrame):
             # Wandle Index (Timestamp) in eine Spalte um, bevor es in Dict umgewandelt wird
             ec_df = final_result_serializable['equity_curve'].reset_index()
             ec_df['timestamp'] = ec_df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S%z') # ISO Format mit TZ
             final_result_serializable['equity_curve_list'] = ec_df.to_dict('records')
             del final_result_serializable['equity_curve']

        # Serialisiere pnl_per_strategy und trades_per_strategy DataFrames
        for key in ['pnl_per_strategy', 'trades_per_strategy']:
            if key in final_result_serializable and isinstance(final_result_serializable[key], pd.DataFrame):
                final_result_serializable[key] = final_result_serializable[key].to_dict('records')

        # Serialisiere einzelne Timestamps
        for key in ['max_drawdown_date', 'liquidation_date']:
            if final_result_serializable.get(key) and isinstance(final_result_serializable[key], pd.Timestamp):
                 final_result_serializable[key] = final_result_serializable[key].strftime('%Y-%m-%d %H:%M:%S%z')


        save_data = {
            "optimal_portfolio": best_portfolio_ids,
            "final_summary": final_result_serializable # Verwende die serialisierte Version
        }
        with open(results_file_path, 'w') as f:
            json.dump(save_data, f, indent=4, default=str) # default=str als Fallback
        logger.info(f"Optimierungsergebnisse gespeichert in: {results_file_path}")

    except Exception as e:
        logger.error(f"Fehler beim Speichern der Optimierungsergebnisse als JSON: {e}", exc_info=True)


    # Gib das Original-Ergebnis mit DataFrame zurück für show_results.py
    return {"optimal_portfolio": best_portfolio_ids, "final_result": best_portfolio_result}
