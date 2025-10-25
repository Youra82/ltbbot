# src/ltbbot/analysis/portfolio_optimizer.py
import pandas as pd
import itertools
from tqdm import tqdm
import logging
import os
import sys
import json # Added for saving results

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# Import the portfolio simulator which internally uses the envelope backtester
from ltbbot.analysis.portfolio_simulator import run_portfolio_simulation

# --- Portfolio Optimizer Logic (Greedy Approach) ---
def run_portfolio_optimizer(start_capital, strategies_data, start_date, end_date):
    """
    Findet eine gute Kombination von Envelope-Strategien mithilfe eines Greedy-Algorithmus.
    Optimierungsziel: Maximierung einer risikoadjustierten Metrik (z.B. vereinfachte Calmar Ratio).

    Args:
        start_capital (float): Startkapital.
        strategies_data (dict): Dict mapping strategy_id to {'symbol', 'timeframe', 'data': pd.DataFrame, 'params': dict}.
        start_date (str): Startdatum JJJJ-MM-TT.
        end_date (str): Enddatum JJJJ-MM-TT.

    Returns:
        dict: {'optimal_portfolio': [list of strategy_ids], 'final_result': dict} or None.
    """
    logger.info("\n--- Starte automatische Portfolio-Optimierung (Envelope)... ---")

    if not strategies_data:
        logger.error("Keine Strategien zum Optimieren übergeben.")
        return None

    # --- 1. Einzel-Performance analysieren ---
    logger.info("1/3: Analysiere Einzel-Performance jeder Strategie...")
    single_strategy_results = []

    # Verwende strategy_id (Dateiname) als Schlüssel
    for strategy_id, strat_data in tqdm(strategies_data.items(), desc="Bewerte Einzelstrategien"):
        # Simulator erwartet ein Dict {strategy_id: strat_data}
        sim_data_single = {strategy_id: strat_data}
        try:
            # Führe Simulation für nur diese eine Strategie durch
            result = run_portfolio_simulation(start_capital, sim_data_single, start_date, end_date)

            # Bewerte nur profitable und nicht-liquidierte Strategien positiv
            if result and result.get("end_capital", 0) > 0 and not result.get("liquidation_date"):
                pnl_pct = result.get('total_pnl_pct', -100.0)
                # Max Drawdown als positiven Wert für Score-Berechnung nehmen
                max_dd_pct = result.get('max_drawdown_pct', 100.0)
                if max_dd_pct < 0.1: max_dd_pct = 0.1 # Minimalwert, um Division durch Null zu vermeiden

                # Score = Vereinfachte Calmar Ratio (PnL / Max Drawdown)
                # Hoher PNL bei niedrigem DD ist gut
                score = pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct if pnl_pct > 0 else -1000

                single_strategy_results.append({
                    'strategy_id': strategy_id,
                    'score': score,
                    'pnl_pct': pnl_pct,
                    'max_dd_pct': max_dd_pct,
                    'result': result # Speichere das vollständige Ergebnis
                })
            else:
                # Logge, warum eine Strategie ausgeschlossen wird (optional)
                reason = "Liquidiert" if result and result.get("liquidation_date") else "Nicht profitabel/Fehler"
                logger.debug(f"Strategie {strategy_id} ausgeschlossen: {reason}")


        except Exception as e:
            logger.error(f"Fehler bei Einzel-Simulation von {strategy_id}: {e}", exc_info=False)
            continue # Nächste Strategie

    if not single_strategy_results:
        logger.error("Keine einzige Strategie war für sich allein profitabel und überlebensfähig. Portfolio-Optimierung nicht möglich.")
        return None

    # --- 2. Greedy-Algorithmus: Bestes Team aufbauen ---
    # Sortiere nach bestem Score, um den "Star-Spieler" zu finden
    single_strategy_results.sort(key=lambda x: x['score'], reverse=True)

    best_portfolio_ids = [single_strategy_results[0]['strategy_id']]
    best_portfolio_score = single_strategy_results[0]['score']
    best_portfolio_result = single_strategy_results[0]['result'] # Ergebnis der besten Einzelstrategie

    logger.info(f"2/3: Star-Spieler gefunden: {best_portfolio_ids[0]} (Score: {best_portfolio_score:.2f})")
    logger.info("3/3: Suche die besten Team-Kollegen...")

    # Pool der verbleibenden Kandidaten
    candidate_pool = [res['strategy_id'] for res in single_strategy_results[1:]]

    # Greedy-Loop: Füge schrittweise die beste nächste Strategie hinzu
    while True:
        best_next_addition_id = None
        best_score_with_addition = best_portfolio_score # Start mit Score des aktuellen besten Teams
        current_best_result_with_addition = best_portfolio_result

        progress_bar = tqdm(candidate_pool, desc=f"Teste Team mit {len(best_portfolio_ids)+1} Mitgliedern", leave=False)
        for candidate_id in progress_bar:
            current_potential_team_ids = best_portfolio_ids + [candidate_id]

            # --- Validierungs-Check (optional, aber empfohlen): ---
            # Verhindere Duplikate (gleiches Symbol/Timeframe im Team)
            unique_check_set = set()
            is_valid_team = True
            for team_member_id in current_potential_team_ids:
                # Extrahiere Symbol/Timeframe aus den ursprünglichen Daten
                member_data = strategies_data.get(team_member_id)
                if not member_data: continue # Sollte nicht passieren
                symbol_tf_key = f"{member_data['symbol']}_{member_data['timeframe']}"
                if symbol_tf_key in unique_check_set:
                    is_valid_team = False
                    # logger.debug(f"Ungültiges Team (Duplikat): {symbol_tf_key} in {current_potential_team_ids}")
                    break
                unique_check_set.add(symbol_tf_key)

            if not is_valid_team:
                continue # Nächsten Kandidaten prüfen

            # Stelle die Daten für den Simulator zusammen
            current_team_data = {
                team_member_id: strategies_data[team_member_id]
                for team_member_id in current_potential_team_ids
            }

            # --- Führe Simulation für das potenzielle Team durch ---
            try:
                result = run_portfolio_simulation(start_capital, current_team_data, start_date, end_date)

                # Bewerte das Ergebnis
                if result and result.get("end_capital", 0) > 0 and not result.get("liquidation_date"):
                    pnl_pct = result.get('total_pnl_pct', -100.0)
                    max_dd_pct = result.get('max_drawdown_pct', 100.0)
                    if max_dd_pct < 0.1: max_dd_pct = 0.1 # Minimalwert

                    score = pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct if pnl_pct > 0 else -1000

                    # Wenn dieser Kandidat das Team verbessert, merke ihn dir
                    if score > best_score_with_addition:
                        best_score_with_addition = score
                        best_next_addition_id = candidate_id
                        current_best_result_with_addition = result # Speichere das Ergebnis dieses besten Teams
                        # Update Fortschrittsanzeige (optional)
                        # progress_bar.set_postfix(best_score=f"{score:.2f}")

                # else: # Logge liquidierte/unprofitable Teams (optional, kann viel Output erzeugen)
                    # logger.debug(f"Team {current_potential_team_ids} nicht erfolgreich (Score < {best_score_with_addition:.2f} oder liquidiert).")

            except Exception as e:
                logger.error(f"Fehler bei Simulation von Team {current_potential_team_ids}: {e}", exc_info=False)
                continue # Nächsten Kandidaten prüfen


        # --- Entscheide, ob das Team erweitert wird ---
        if best_next_addition_id:
            logger.info(f"-> Füge hinzu: {best_next_addition_id} (Neuer Score: {best_score_with_addition:.2f})")
            best_portfolio_ids.append(best_next_addition_id)
            best_portfolio_score = best_score_with_addition
            best_portfolio_result = current_best_result_with_addition # Update das beste Ergebnis
            candidate_pool.remove(best_next_addition_id) # Entferne aus Kandidaten
            if not candidate_pool: # Wenn keine Kandidaten mehr übrig sind
                 logger.info("Alle Kandidaten getestet.")
                 break
        else:
            # Keine weitere Verbesserung durch Hinzufügen gefunden.
            logger.info("Keine weitere Verbesserung durch Hinzufügen von Strategien gefunden. Optimierung beendet.")
            break # Greedy-Loop beenden

    # --- Endergebnis zurückgeben ---
    logger.info(f"Optimierung abgeschlossen. Bestes Portfolio hat {len(best_portfolio_ids)} Strategien mit Score {best_portfolio_score:.2f}.")

    # Speichere das Ergebnis optional als JSON
    results_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
    os.makedirs(results_dir, exist_ok=True)
    results_file_path = os.path.join(results_dir, 'portfolio_optimization_results.json')
    try:
        # Equity Curve DataFrame ist nicht direkt JSON-serialisierbar
        final_result_serializable = best_portfolio_result.copy()
        if 'equity_curve' in final_result_serializable and isinstance(final_result_serializable['equity_curve'], pd.DataFrame):
             # Wandle Timestamps um oder entferne die Kurve für JSON
             # Option 1: Entfernen
             # del final_result_serializable['equity_curve']
             # Option 2: Konvertieren (kann groß werden)
             equity_list = final_result_serializable['equity_curve'].to_dict('records')
             # Konvertiere Timestamps in Strings
             for record in equity_list:
                  record['timestamp'] = record['timestamp'].isoformat()
             final_result_serializable['equity_curve_list'] = equity_list # Als Liste speichern
             del final_result_serializable['equity_curve'] # DataFrame entfernen

        # Gleiches für pnl/trades DataFrames
        if 'pnl_per_strategy' in final_result_serializable and isinstance(final_result_serializable['pnl_per_strategy'], pd.DataFrame):
             final_result_serializable['pnl_per_strategy'] = final_result_serializable['pnl_per_strategy'].to_dict('records')
        if 'trades_per_strategy' in final_result_serializable and isinstance(final_result_serializable['trades_per_strategy'], pd.DataFrame):
             final_result_serializable['trades_per_strategy'] = final_result_serializable['trades_per_strategy'].to_dict('records')

        # Konvertiere Datumsangaben in Strings
        if final_result_serializable.get('max_drawdown_date'):
             final_result_serializable['max_drawdown_date'] = final_result_serializable['max_drawdown_date'].isoformat()
        if final_result_serializable.get('liquidation_date'):
             final_result_serializable['liquidation_date'] = final_result_serializable['liquidation_date'].isoformat()


        save_data = {
             "optimal_portfolio": best_portfolio_ids,
             "final_summary": final_result_serializable # Verwende das serialisierbare Ergebnis
        }
        with open(results_file_path, 'w') as f:
             json.dump(save_data, f, indent=4)
        logger.info(f"Optimierungsergebnisse gespeichert in: {results_file_path}")

    except Exception as e:
        logger.error(f"Fehler beim Speichern der Optimierungsergebnisse: {e}")


    # Gib das Original-Ergebnis mit DataFrame zurück für show_results.py
    return {"optimal_portfolio": best_portfolio_ids, "final_result": best_portfolio_result}
