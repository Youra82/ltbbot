# src/ltbbot/analysis/show_results.py
import os
import sys
import json
import pandas as pd
from datetime import date
import logging
import argparse

# Suppress verbose TensorFlow/Keras logs (falls vorhanden)
logging.getLogger('tensorflow').setLevel(logging.ERROR)
logging.getLogger('absl').setLevel(logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # Suppress TF messages

# --- Project Path Setup ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# --- ltbbot Imports ---
from ltbbot.analysis.backtester import load_data, run_envelope_backtest
from ltbbot.analysis.portfolio_simulator import run_portfolio_simulation
from ltbbot.analysis.portfolio_optimizer import run_portfolio_optimizer
from ltbbot.analysis.evaluator import evaluate_dataset
from ltbbot.utils.telegram import send_document

# --- Setup Logger ---
# Verwende logging.basicConfig, um das Root-Logging zu konfigurieren
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
# Erstelle einen spezifischen Logger für dieses Skript
logger = logging.getLogger("show_results")


# --- Mode 1: Einzel-Analyse ---
def run_single_analysis(start_date, end_date, start_capital):
    logger.info("--- ltbbot Ergebnis-Analyse (Einzel-Modus) ---")

    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    results_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'results') # Store results here
    os.makedirs(results_dir, exist_ok=True)

    all_results_summary = []

    try:
        config_files = sorted([
            f for f in os.listdir(configs_dir)
            if f.startswith('config_') and f.endswith('_envelope.json')
        ])
    except FileNotFoundError:
           logger.error(f"Konfigurationsverzeichnis nicht gefunden: {configs_dir}")
           return

    if not config_files:
        logger.warning("\nKeine gültigen Envelope-Konfigurationen zum Analysieren gefunden.")
        return

    logger.info(f"Gefundene Konfigurationen: {len(config_files)}")

    for filename in config_files:
        config_path = os.path.join(configs_dir, filename)
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)

            symbol = config['market']['symbol']
            timeframe = config['market']['timeframe']
            strategy_name = f"{symbol} ({timeframe})"

            logger.info(f"\nAnalysiere Ergebnisse für: {filename}...")

            data = load_data(symbol, timeframe, start_date, end_date)
            if data is None or data.empty:
                logger.warning(f"--> Konnte keine Daten für {strategy_name} laden. Überspringe.")
                continue

            result = run_envelope_backtest(data.copy(), config, start_capital)

            if result:
                 # *** NEU: Status prüfen ***
                 max_dd = result.get('max_drawdown_pct', 100)
                 end_capital_val = result.get('end_capital', 0)
                 status = "⚠️ Liquidiert" if max_dd >= 100 or end_capital_val <= 0 else "OK"
                 # **************************

                 summary_entry = {
                     "Strategie": strategy_name,
                     "Startkapital": result.get('start_capital', start_capital),
                     "Endkapital": end_capital_val,
                     "PnL %": result.get('total_pnl_pct', 0),
                     "Max DD %": max_dd,
                     "Trades": result.get('trades_count', 0),
                     "Win Rate %": result.get('win_rate', 0),
                     "Status": status # *** NEU: Status hinzufügen ***
                 }
                 all_results_summary.append(summary_entry)
                 # Logge auch den Status
                 logger.info(f"--> Ergebnis: PnL={summary_entry['PnL %']:.2f}%, DD={summary_entry['Max DD %']:.2f}%, Trades={summary_entry['Trades']}, Status={status}")
            else:
                 logger.warning(f"--> Backtest für {strategy_name} fehlgeschlagen oder keine Ergebnisse.")

        except FileNotFoundError:
            logger.warning(f"--> Konfigurationsdatei {filename} nicht gefunden während der Analyse. Überspringe.")
        except json.JSONDecodeError:
            logger.warning(f"--> Fehler beim Lesen der JSON-Datei {filename}. Überspringe.")
        except Exception as e:
            logger.error(f"--> Unerwarteter Fehler bei der Analyse von {filename}: {e}", exc_info=True)


    # --- Ergebnisse anzeigen und speichern ---
    if not all_results_summary:
        logger.warning("\nKeine gültigen Backtest-Ergebnisse zum Anzeigen vorhanden.")
        return

    results_df = pd.DataFrame(all_results_summary)

    # Sortiere nach PnL % (absteigend)
    results_df = results_df.sort_values(by="PnL %", ascending=False)

    # Spalten formatieren für bessere Lesbarkeit in der Konsole
    results_df_display = results_df.copy()
    results_df_display["Startkapital"] = results_df_display["Startkapital"].map('{:,.0f}'.format)
    results_df_display["Endkapital"] = results_df_display["Endkapital"].map('{:,.2f}'.format)
    results_df_display["PnL %"] = results_df_display["PnL %"].map('{:.2f}%'.format)
    results_df_display["Max DD %"] = results_df_display["Max DD %"].map('{:.2f}%'.format)
    results_df_display["Win Rate %"] = results_df_display["Win Rate %"].map('{:.2f}%'.format)

    # *** NEU: Status-Spalte in Anzeigeliste aufnehmen ***
    display_columns = ["Strategie", "Startkapital", "Endkapital", "PnL %", "Max DD %", "Trades", "Win Rate %", "Status"]
    # Sicherstellen, dass nur existierende Spalten angezeigt werden (falls eine fehlt)
    existing_display_columns = [col for col in display_columns if col in results_df_display.columns]

    # Ergebnisse in der Konsole ausgeben
    pd.set_option('display.width', 120) # Breite anpassen, falls nötig
    pd.set_option('display.max_columns', None)
    print("\n\n" + "="*120) # Breite anpassen
    print(f"  Zusammenfassung Einzel-Analysen ({start_date} bis {end_date})")
    print("="*120) # Breite anpassen
    # Verwende die formatierte DataFrame und die angepasste Spaltenliste
    print(results_df_display.to_string(index=False, columns=existing_display_columns))
    print("="*120) # Breite anpassen

    # Ergebnisse als CSV speichern (mit numerischen Werten und Status)
    # Verwende die *unformatierte* DataFrame results_df
    results_csv_path = os.path.join(PROJECT_ROOT, f'single_analysis_summary_{date.today()}.csv')
    try:
        # Stelle sicher, dass die Status-Spalte auch in der CSV ist
        csv_columns = ["Strategie", "Startkapital", "Endkapital", "PnL %", "Max DD %", "Trades", "Win Rate %", "Status"]
        existing_csv_columns = [col for col in csv_columns if col in results_df.columns]
        results_df[existing_csv_columns].to_csv(results_csv_path, index=False, float_format='%.2f')
        logger.info(f"✔ Zusammenfassung gespeichert unter: {results_csv_path}")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Zusammenfassung als CSV: {e}")

# --- Mode 2 & 3: Portfolio-Simulation / Optimierung ---
# (Dieser Teil bleibt unverändert, da der Portfolio-Simulator bereits Liquidationsdaten liefert
# und der Optimierer liquidierte Einzelstrategien ignoriert)
def run_portfolio_mode(is_auto: bool, start_date, end_date, start_capital):
    mode_name = "Automatische Portfolio-Optimierung" if is_auto else "Manuelle Portfolio-Simulation"
    logger.info(f"--- ltbbot {mode_name} ---")

    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    results_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
    os.makedirs(results_dir, exist_ok=True)

    available_strategy_configs = {}

    try:
        config_files = sorted([
            f for f in os.listdir(configs_dir)
            if f.startswith('config_') and f.endswith('_envelope.json')
        ])
        if not config_files:
            logger.warning("Keine optimierten Strategien (config_*_envelope.json) gefunden.")
            return

        for filename in config_files:
            config_path = os.path.join(configs_dir, filename)
            try:
                with open(config_path, 'r') as f: config = json.load(f)
                if 'market' in config and 'symbol' in config['market'] and 'timeframe' in config['market']:
                    available_strategy_configs[filename] = config
                else: logger.warning(f"Überspringe ungültige Konfiguration: {filename}")
            except Exception as e: logger.warning(f"Fehler beim Laden von {filename}: {e}")

    except FileNotFoundError:
        logger.error(f"Konfigurationsverzeichnis nicht gefunden: {configs_dir}"); return

    if not available_strategy_configs:
            logger.warning("Keine gültigen Strategie-Konfigurationen gefunden."); return

    selected_files = []
    available_filenames = list(available_strategy_configs.keys())
    max_portfolio_dd = 0.30 # Standard

    if is_auto:
        try:
             max_dd_input = input(f"Maximal erlaubter Drawdown für das PORTFOLIO in % eingeben [Standard: {max_portfolio_dd*100:.0f}]: ")
             if max_dd_input:
                  max_portfolio_dd = float(max_dd_input) / 100.0
                  if not (0 < max_portfolio_dd <= 1): raise ValueError("Drawdown muss > 0 und <= 100 sein.")
             print(f"Verwende maximalen Portfolio Drawdown von {max_portfolio_dd*100:.1f}% für die Optimierung.")
        except ValueError as e:
             print(f"Ungültige Eingabe für Drawdown: {e}. Verwende Standardwert {max_portfolio_dd*100:.0f}%.")
        selected_files = available_filenames
    else: # Manueller Modus
        print("\nVerfügbare optimierte Strategien:")
        for i, name in enumerate(available_filenames):
            config = available_strategy_configs[name]
            print(f"  {i+1}) {config['market']['symbol']} ({config['market']['timeframe']}) - [{name}]")
        selection = input("\nWelche Strategien sollen simuliert werden? (Zahlen mit Komma, z.B. 1,3,4 oder 'alle'): ").strip()
        try:
            if selection.lower() == 'alle': selected_files = available_filenames
            else:
                indices = [int(i.strip()) - 1 for i in selection.split(',')]
                if any(i < 0 or i >= len(available_filenames) for i in indices): raise IndexError("Auswahl außerhalb des gültigen Bereichs.")
                selected_files = [available_filenames[i] for i in indices]
        except (ValueError, IndexError) as e: print(f"Ungültige Auswahl: {e}. Breche ab."); return

    if not selected_files: logger.warning("Keine Strategien ausgewählt."); return

    strategies_data_for_sim = {}
    logger.info("\nLade Daten für gewählte Strategien...")
    for filename in selected_files:
        config = available_strategy_configs[filename]
        symbol, timeframe = config['market']['symbol'], config['market']['timeframe']
        data = load_data(symbol, timeframe, start_date, end_date)
        if data is not None and not data.empty:
            strategies_data_for_sim[filename] = {
                'symbol': symbol, 'timeframe': timeframe, 'data': data, 'params': config
            }
        else: logger.warning(f"Konnte Daten für {filename} nicht laden. Wird ignoriert.")

    if not strategies_data_for_sim: logger.error("Konnte für keine der gewählten Strategien Daten laden."); return

    equity_df = pd.DataFrame()
    report_csv_path = ""
    report_caption = ""
    results = None

    if is_auto:
        try:
            logger.info("Starte automatische Portfolio-Optimierung...")
            results = run_portfolio_optimizer(start_capital, strategies_data_for_sim, start_date, end_date, max_portfolio_dd_constraint=max_portfolio_dd)
            if results and 'final_result' in results and 'optimal_portfolio' in results:
                final_report = results['final_result']
                optimal_portfolio_ids = results['optimal_portfolio']
                print("\n" + "="*60); print("  Ergebnis der automatischen Portfolio-Optimierung"); print("="*60)
                print(f"Zeitraum: {start_date} bis {end_date}\nStartkapital: {start_capital:,.2f} USDT\nMax Portfolio DD Constraint: {max_portfolio_dd*100:.1f}%")
                print(f"\nOptimales Portfolio gefunden ({len(optimal_portfolio_ids)} Strategien):")
                for strat_id in optimal_portfolio_ids:
                    cfg = available_strategy_configs.get(strat_id, {}); mkt = cfg.get('market', {})
                    print(f"  - {mkt.get('symbol', 'N/A')} ({mkt.get('timeframe', 'N/A')})")
                print("\n--- Simulierte Performance dieses optimalen Portfolios ---")
                print(f"Endkapital:       {final_report.get('end_capital', 0):,.2f} USDT")
                pnl_usd = final_report.get('end_capital', 0) - start_capital; pnl_pct = final_report.get('total_pnl_pct', 0)
                print(f"Gesamt PnL:       {pnl_usd:+,.2f} USDT ({pnl_pct:.2f}%)")
                print(f"Portfolio Max DD: {final_report.get('max_drawdown_pct', 100):.2f}%")
                liq_date = final_report.get('liquidation_date')
                print(f"Liquidiert:       {'JA, am ' + liq_date.strftime('%Y-%m-%d') if liq_date and isinstance(liq_date, pd.Timestamp) else 'NEIN'}")
                print("="*60)
                equity_df = final_report.get('equity_curve')
                report_csv_path = os.path.join(PROJECT_ROOT, 'optimal_portfolio_equity.csv')
                report_caption = f"Optimales Portfolio ({len(optimal_portfolio_ids)} Strategien)\n{start_date} bis {end_date}\nEndkapital: {final_report.get('end_capital', 0):,.2f} USDT"

                save_optimal_to_settings = input("\nSollen diese optimalen Strategien in settings.json als aktiv markiert werden? (j/n) [n]: ").lower() == 'j'
                if save_optimal_to_settings:
                    try:
                        settings_path = os.path.join(PROJECT_ROOT, 'settings.json')
                        with open(settings_path, 'r') as f: settings_data = json.load(f)
                        new_active_strategies = []
                        for fname in optimal_portfolio_ids:
                            cfg = available_strategy_configs.get(fname)
                            if cfg: new_active_strategies.append({"symbol": cfg['market']['symbol'], "timeframe": cfg['market']['timeframe'], "active": True})
                        if 'live_trading_settings' not in settings_data: settings_data['live_trading_settings'] = {}
                        settings_data['live_trading_settings']['active_strategies'] = new_active_strategies
                        with open(settings_path, 'w') as f: json.dump(settings_data, f, indent=4)
                        logger.info(f"✔ Optimale Strategien in '{settings_path}' als aktiv gespeichert.")
                    except Exception as e: logger.error(f"Fehler beim Speichern in settings.json: {e}")
            else: logger.error("Portfolio-Optimierung fehlgeschlagen.")
        except Exception as e: logger.error(f"Fehler während Portfolio-Optimierung: {e}", exc_info=True)

    else: # Manueller Modus
        try:
            logger.info(f"Starte manuelle Portfolio-Simulation für {len(strategies_data_for_sim)} Strategien...")
            results = run_portfolio_simulation(start_capital, strategies_data_for_sim, start_date, end_date)
            if results:
                print("\n" + "="*60); print("  Portfolio-Simulations-Ergebnis (Manuell)"); print("="*60)
                print(f"Zeitraum: {start_date} bis {end_date}\nStartkapital: {results.get('start_capital', start_capital):,.2f} USDT")
                print("\n--- Gesamt-Performance ---")
                print(f"Endkapital:       {results.get('end_capital', 0):,.2f} USDT")
                pnl_usd = results.get('end_capital', 0) - results.get('start_capital', start_capital); pnl_pct = results.get('total_pnl_pct', 0)
                print(f"Gesamt PnL:       {pnl_usd:+,.2f} USDT ({pnl_pct:.2f}%)")
                print(f"Anzahl Trades:    {results.get('trade_count', 0)}")
                print(f"Win-Rate:         {results.get('win_rate', 0):.2f}%")
                mdd_pct = results.get('max_drawdown_pct', 100); mdd_date = results.get('max_drawdown_date')
                mdd_date_str = mdd_date.strftime('%Y-%m-%d') if mdd_date and isinstance(mdd_date, pd.Timestamp) else 'N/A'
                print(f"Portfolio Max DD: {mdd_pct:.2f}% (am {mdd_date_str})")
                liq_date = results.get('liquidation_date')
                print(f"Liquidiert:       {'JA, am ' + liq_date.strftime('%Y-%m-%d') if liq_date and isinstance(liq_date, pd.Timestamp) else 'NEIN'}")
                print("="*60)
                equity_df = results.get('equity_curve')
                report_csv_path = os.path.join(PROJECT_ROOT, 'manual_portfolio_equity.csv')
                report_caption = f"Manuelle Simulation ({len(strategies_data_for_sim)} Strategien)\n{start_date} bis {end_date}\nEndkapital: {results.get('end_capital', 0):,.2f} USDT"
            else: logger.error("Portfolio-Simulation fehlgeschlagen.")
        except Exception as e: logger.error(f"Fehler während manueller Simulation: {e}", exc_info=True)


    # --- Zentraler Export und Telegram-Versand ---
    if equity_df is not None and not equity_df.empty and report_csv_path:
        print("\n--- Export ---")
        try:
            if not isinstance(equity_df.index, pd.DatetimeIndex) and 'timestamp' in equity_df.columns:
                 equity_df = equity_df.set_index('timestamp')
            elif not isinstance(equity_df.index, pd.DatetimeIndex):
                 logger.error("Equity Curve DataFrame hat keinen gültigen Zeitstempel-Index/Spalte.")
                 return

            export_cols = ['equity', 'drawdown_pct']
            missing_cols = [col for col in export_cols if col not in equity_df.columns]
            if missing_cols: logger.warning(f"Spalten fehlen für CSV-Export: {missing_cols}. Exportiere verfügbare.")
            export_cols = [col for col in export_cols if col in equity_df.columns]

            if export_cols:
                equity_df[export_cols].to_csv(report_csv_path, index=True, float_format='%.2f')
                logger.info(f"✔ Details zur Equity-Kurve wurden nach '{os.path.basename(report_csv_path)}' exportiert.")
                try:
                    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
                    with open(secret_path, 'r') as f: secrets = json.load(f)
                    telegram_config = secrets.get('telegram', {})
                    if telegram_config.get('bot_token') and telegram_config.get('chat_id'):
                        logger.info("Sende Bericht an Telegram...")
                        send_document(telegram_config['bot_token'], telegram_config['chat_id'], report_csv_path, report_caption)
                        logger.info("✔ Bericht wurde erfolgreich an Telegram gesendet.")
                    else: logger.warning("Telegram nicht konfiguriert. Kein Versand.")
                except FileNotFoundError: logger.error(f"secret.json nicht gefunden für Telegram.")
                except Exception as e: logger.error(f"ⓘ Konnte Bericht nicht an Telegram senden: {e}")
            else: logger.warning("Keine gültigen Spalten zum Exportieren.")

        except Exception as e: logger.error(f"Fehler beim Exportieren der Equity Curve: {e}", exc_info=True)
    elif results:
        logger.warning("\nKeine Equity-Daten zum Exportieren vorhanden.")


# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ltbbot Backtest Ergebnis-Analyse")
    parser.add_argument('--mode', default='1', type=str, choices=['1', '2', '3'],
                        help="Analysemodus: 1=Einzel, 2=Manuell Portfolio, 3=Auto Portfolio")
    args = parser.parse_args()

    print("\n--- Bitte Konfiguration für den Backtest festlegen ---")
    try:
        start_date_input = input(f"Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: ") or "2023-01-01"
        end_date_input = input(f"Enddatum (JJJJ-MM-TT) [Standard: Heute]: ") or date.today().strftime("%Y-%m-%d")
        start_capital_input = int(input(f"Startkapital in USDT eingeben [Standard: 1000]: ") or 1000)
        print("--------------------------------------------------")

        if start_capital_input <= 0: raise ValueError("Startkapital muss positiv sein.")
        pd.to_datetime(start_date_input); pd.to_datetime(end_date_input) # Einfache Datumsvalidierung

        if args.mode == '2':
            run_portfolio_mode(is_auto=False, start_date=start_date_input, end_date=end_date_input, start_capital=start_capital_input)
        elif args.mode == '3':
            run_portfolio_mode(is_auto=True, start_date=start_date_input, end_date=end_date_input, start_capital=start_capital_input)
        else: # mode == '1'
            run_single_analysis(start_date=start_date_input, end_date=end_date_input, start_capital=start_capital_input)

    except ValueError as e: print(f"Ungültige Eingabe: {e}")
    except Exception as e: logger.critical(f"Ein unerwarteter Fehler ist aufgetreten: {e}", exc_info=True)
