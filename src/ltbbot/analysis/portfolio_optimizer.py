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
    Stellt sicher, dass jedes Symbol nur einmal im Portfolio vorkommt.

    Args:
        start_capital (float): Startkapital.
        strategies_data (dict): Dict mapping strategy_id to {'symbol', 'timeframe', 'data': pd.DataFrame, 'params': dict}.
        start_date (str): Startdatum JJJJ-MM-TT.
        end_date (str): Enddatum JJJJ-MM-TT.

    Returns:
        dict: {'optimal_portfolio': [list of strategy_ids], 'final_result': dict} or None.
    """
    logger.info("\n--- Starte automatische Portfolio-Optimierung (Envelope, Symbol-Exklusiv)... ---") # Hinweis hinzugefügt

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
                score = pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct if pnl_pct > 0 else -1000

                single_strategy_results.append({
                    'strategy_id': strategy_id,
                    'symbol': strat_data['symbol'], # Symbol hinzufügen für spätere Prüfung
                    'score': score,
                    'pnl_pct': pnl_pct,
                    'max_dd_pct': max_dd_pct,
                    'result': result # Speichere das vollständige Ergebnis
                })
            else:
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
    best_portfolio_symbols = {single_strategy_results[0]['symbol']} # Set mit Symbolen im besten Portfolio
    best_portfolio_score = single_strategy_results[0]['score']
    best_portfolio_result = single_strategy_results[0]['result'] # Ergebnis der besten Einzelstrategie

    logger.info(f"2/3: Star-Spieler gefunden: {best_portfolio_ids[0]} (Score: {best_portfolio_score:.2f})")
    logger.info("3/3: Suche die besten Team-Kollegen...")

    # Pool der verbleibenden Kandidaten (als Dictionaries, um Symbol zu haben)
    candidate_pool = single_strategy_results[1:]

    # Greedy-Loop: Füge schrittweise die beste nächste Strategie hinzu
    while True:
        best_next_addition_candidate = None # Speichert das ganze Kandidaten-Dict
        best_score_with_addition = best_portfolio_score # Start mit Score des aktuellen besten Teams
        current_best_result_with_addition = best_portfolio_result

        # Iteriere durch Kopie, um Elemente sicher entfernen zu können
        candidates_to_evaluate = list(candidate_pool)
        progress_bar = tqdm(candidates_to_evaluate, desc=f"Teste Team mit {len(best_portfolio_ids)+1} Mitgliedern", leave=False)

        for candidate in progress_bar:
            candidate_id = candidate['strategy_id']
            candidate_symbol = candidate['symbol']

            # --- KORRIGIERTER Validierungs-Check: Symbol-Exklusivität ---
            if candidate_symbol in best_portfolio_symbols:
                # logger.debug(f"Überspringe Kandidat {candidate_id}, da Symbol {candidate_symbol} bereits im Portfolio ist.")
                continue # Nächsten Kandidaten prüfen

            # Potenzielle neue Team-Zusammensetzung
            current_potential_team_ids = best_portfolio_ids + [candidate_id]

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
                    if max_dd_pct < 0.1: max_dd_pct = 0.1

                    score = pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct if pnl_pct > 0 else -1000

                    # Wenn dieser Kandidat das Team verbessert, merke ihn dir
                    if score > best_score_with_addition:
                        best_score_with_addition = score
                        best_next_addition_candidate = candidate # Merke den ganzen Kandidaten
                        current_best_result_with_addition = result

            except Exception as e:
                logger.error(f"Fehler bei Simulation von Team mit {candidate_id}: {e}", exc_info=False)
                continue # Nächsten Kandidaten prüfen

        # --- Entscheide, ob das Team erweitert wird ---
        if best_next_addition_candidate:
            best_next_id = best_next_addition_candidate['strategy_id']
            best_next_symbol = best_next_addition_candidate['symbol']

            logger.info(f"-> Füge hinzu: {best_next_id} (Neuer Score: {best_score_with_addition:.2f})")
            best_portfolio_ids.append(best_next_id)
            best_portfolio_symbols.add(best_next_symbol) # Symbol zum Set hinzufügen
            best_portfolio_score = best_score_with_addition
            best_portfolio_result = current_best_result_with_addition # Update das beste Ergebnis

            # Entferne den hinzugefügten Kandidaten aus dem Pool für die nächste Runde
            candidate_pool = [c for c in candidate_pool if c['strategy_id'] != best_next_id]

            if not candidate_pool: # Wenn keine Kandidaten mehr übrig sind
                logger.info("Alle Kandidaten getestet.")
                break
        else:
            # Keine weitere Verbesserung durch Hinzufügen gefunden.
            logger.info("Keine weitere Verbesserung durch Hinzufügen von Strategien gefunden (unter Berücksichtigung der Symbol-Exklusivität). Optimierung beendet.")
            break # Greedy-Loop beenden

    # --- Endergebnis zurückgeben ---
    logger.info(f"Optimierung abgeschlossen. Bestes Portfolio hat {len(best_portfolio_ids)} Strategien mit Score {best_portfolio_score:.2f}.")

    # Speichere das Ergebnis optional als JSON
    results_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
    os.makedirs(results_dir, exist_ok=True)
    results_file_path = os.path.join(results_dir, 'portfolio_optimization_results.json')
    try:
        final_result_serializable = best_portfolio_result.copy()
        # --- Serialisierung ( Equity Curve etc. ) ---
        if 'equity_curve' in final_result_serializable and isinstance(final_result_serializable['equity_curve'], pd.DataFrame):
            equity_list = final_result_serializable['equity_curve'].to_dict('records')
            for record in equity_list:
                if isinstance(record.get('timestamp'), pd.Timestamp): # Sicherstellen, dass es Timestamp ist
                     record['timestamp'] = record['timestamp'].isoformat()
            final_result_serializable['equity_curve_list'] = equity_list
            del final_result_serializable['equity_curve']

        if 'pnl_per_strategy' in final_result_serializable and isinstance(final_result_serializable['pnl_per_strategy'], pd.DataFrame):
            final_result_serializable['pnl_per_strategy'] = final_result_serializable['pnl_per_strategy'].to_dict('records')
        if 'trades_per_strategy' in final_result_serializable and isinstance(final_result_serializable['trades_per_strategy'], pd.DataFrame):
            final_result_serializable['trades_per_strategy'] = final_result_serializable['trades_per_strategy'].to_dict('records')

        if final_result_serializable.get('max_drawdown_date') and isinstance(final_result_serializable['max_drawdown_date'], pd.Timestamp):
            final_result_serializable['max_drawdown_date'] = final_result_serializable['max_drawdown_date'].isoformat()
        if final_result_serializable.get('liquidation_date') and isinstance(final_result_serializable['liquidation_date'], pd.Timestamp):
            final_result_serializable['liquidation_date'] = final_result_serializable['liquidation_date'].isoformat()
        # --- Ende Serialisierung ---

        save_data = {
            "optimal_portfolio": best_portfolio_ids,
            "final_summary": final_result_serializable
        }
        with open(results_file_path, 'w') as f:
            json.dump(save_data, f, indent=4)
        logger.info(f"Optimierungsergebnisse gespeichert in: {results_file_path}")

    except Exception as e:
        logger.error(f"Fehler beim Speichern der Optimierungsergebnisse: {e}")


    # Gib das Original-Ergebnis mit DataFrame zurück für show_results.py
    return {"optimal_portfolio": best_portfolio_ids, "final_result": best_portfolio_result}
