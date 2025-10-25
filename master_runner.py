# master_runner.py
import json
import subprocess
import sys
import os
import time
import logging # Logging hinzufügen

# Pfad anpassen, damit die utils importiert werden können
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

# Importiere Exchange nur noch, wenn Balance Check wieder rein soll
# from ltbbot.utils.exchange import Exchange

# Logging Setup (einfach)
log_dir = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'master_runner.log')
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler(log_file), logging.StreamHandler()])

def main():
    """
    Der Master Runner für den ltbbot (Envelope Strategie).
    - Liest die settings.json.
    - Startet für jede als "active" markierte Strategie einen separaten run.py Prozess.
    """
    settings_file = os.path.join(SCRIPT_DIR, 'settings.json')
    # optimization_results_file = os.path.join(SCRIPT_DIR, 'artifacts', 'results', 'optimization_results.json') # Falls Auto-Modus benötigt wird
    bot_runner_script = os.path.join(SCRIPT_DIR, 'src', 'ltbbot', 'strategy', 'run.py')
    secret_file = os.path.join(SCRIPT_DIR, 'secret.json')

    # Finde den Python-Interpreter in der venv
    python_executable = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')
    if not os.path.exists(python_executable):
        logging.critical(f"Fehler: Python-Interpreter in der venv nicht gefunden unter {python_executable}")
        return

    logging.info("=======================================================")
    logging.info("ltbbot Master Runner v1.0 (Envelope)")
    logging.info("=======================================================")

    try:
        with open(settings_file, 'r') as f:
            settings = json.load(f)

        with open(secret_file, 'r') as f:
            secrets = json.load(f)

        if not secrets.get('ltbbot'): # Prüfe auf den richtigen Key
            logging.critical("Fehler: Kein 'ltbbot'-Account in secret.json gefunden.")
            return
        # main_account_config = secrets['ltbbot'][0] # Wird aktuell nicht direkt hier verwendet

        live_settings = settings.get('live_trading_settings', {})
        # use_autopilot = live_settings.get('use_auto_optimizer_results', False) # Optional wieder aktivieren

        strategy_list = []
        # if use_autopilot:
        #     logging.info("Modus: Autopilot. Lese Strategien aus den Optimierungs-Ergebnissen...")
        #     # Implementiere Logik zum Lesen der besten Strategien aus einer Ergebnisdatei
        # else:
        logging.info("Modus: Manuell. Lese Strategien aus den manuellen Einstellungen...")
        strategy_list = live_settings.get('active_strategies', [])

        if not strategy_list:
            logging.warning("Keine aktiven Strategien zum Ausführen gefunden.")
            return

        logging.info("=======================================================")

        active_processes = {} # Verfolgt gestartete Prozesse

        while True: # Endlosschleife zum Überwachen und Neustarten
             processes_to_restart = []
             for i, strategy_info in enumerate(strategy_list):
                 if not isinstance(strategy_info, dict):
                     logging.warning(f"Ungültiger Eintrag in active_strategies (kein Dictionary): {strategy_info}")
                     continue

                 if not strategy_info.get("active", False): # Prüfe, ob aktiv
                     # Wenn ein Prozess lief, aber jetzt inaktiv ist, stoppen (optional)
                     strategy_id = f"{strategy_info.get('symbol', 'N/A')}_{strategy_info.get('timeframe', 'N/A')}"
                     if strategy_id in active_processes and active_processes[strategy_id].poll() is None:
                          logging.info(f"Stoppe inaktiven Prozess für {strategy_id}...")
                          active_processes[strategy_id].terminate()
                          try:
                              active_processes[strategy_id].wait(timeout=5)
                          except subprocess.TimeoutExpired:
                              active_processes[strategy_id].kill()
                          del active_processes[strategy_id]
                     continue # Überspringe inaktive

                 symbol = strategy_info.get('symbol')
                 timeframe = strategy_info.get('timeframe')
                 strategy_id = f"{symbol}_{timeframe}" # Eindeutige ID

                 if not symbol or not timeframe:
                     logging.warning(f"Unvollständige Strategie-Info: {strategy_info}. Überspringe.")
                     continue

                 # Prüfen, ob der Prozess bereits läuft oder neu gestartet werden muss
                 process_needs_start = False
                 if strategy_id not in active_processes:
                     process_needs_start = True
                     logging.info(f"Prozess für {strategy_id} nicht gefunden. Starte neu.")
                 elif active_processes[strategy_id].poll() is not None: # Prozess ist beendet
                     process_needs_start = True
                     exit_code = active_processes[strategy_id].returncode
                     logging.warning(f"Prozess für {strategy_id} wurde beendet (Exit Code: {exit_code}). Starte neu.")
                     del active_processes[strategy_id] # Entferne beendeten Prozess

                 if process_needs_start:
                     logging.info(f"\n--- Starte Bot für: {symbol} ({timeframe}) ---")

                     # Baue den Befehl zusammen
                     command = [
                         python_executable,
                         bot_runner_script,
                         "--symbol", symbol,
                         "--timeframe", timeframe,
                         # Kein --use_macd mehr nötig
                     ]

                     try:
                          # Starte den Prozess
                          process = subprocess.Popen(command)
                          active_processes[strategy_id] = process
                          logging.info(f"Prozess für {strategy_id} gestartet (PID: {process.pid}).")
                          time.sleep(2) # Kurze Pause zwischen Starts
                     except Exception as e:
                          logging.error(f"Fehler beim Starten des Prozesses für {strategy_id}: {e}")

             # Wartezeit vor dem nächsten Check
             logging.debug(f"Aktive Prozesse: {list(active_processes.keys())}. Nächster Check in 60s.")
             time.sleep(60) # Prüfe jede Minute den Status der Prozesse


    except FileNotFoundError as e:
        logging.critical(f"Fehler: Eine wichtige Datei wurde nicht gefunden: {e}")
    except json.JSONDecodeError as e:
         logging.critical(f"Fehler beim Lesen einer JSON-Datei (settings.json oder secret.json): {e}")
    except Exception as e:
        logging.critical(f"Ein unerwarteter Fehler im Master Runner ist aufgetreten: {e}", exc_info=True)

if __name__ == "__main__":
    main()
