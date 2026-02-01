# master_runner.py
import json
import subprocess
import sys
import os
import time
import logging
from datetime import datetime, timedelta

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


def check_and_run_optimizer():
    """
    Prüft ob die automatische Optimierung fällig ist und führt sie ggf. aus.
    
    Wird bei jedem Cron-Job Aufruf einmal geprüft. Die Logik ist tolerant gegenüber
    Cron-Intervallen: Wenn der geplante Zeitpunkt in der Vergangenheit liegt (aber
    noch am selben Tag in der geplanten Stunde), wird die Optimierung gestartet.
    """
    now = datetime.now()
    
    try:
        settings_file = os.path.join(SCRIPT_DIR, 'settings.json')
        with open(settings_file, 'r') as f:
            settings = json.load(f)
        
        opt_settings = settings.get('optimization_settings', {})
        
        # Prüfe ob aktiviert
        if not opt_settings.get('enabled', False):
            return False
        
        schedule = opt_settings.get('schedule', {})
        day_of_week = schedule.get('day_of_week', 0)
        hour = schedule.get('hour', 3)
        minute = schedule.get('minute', 0)
        interval_days = schedule.get('interval_days', 7)
        
        # Prüfe ob heute der richtige Tag ist
        if now.weekday() != day_of_week:
            return False
        
        # Prüfe ob wir in der geplanten Stunde sind (oder danach, aber am gleichen Tag)
        if now.hour < hour:
            return False
        
        # Wenn wir in der richtigen Stunde sind, prüfe ob die Minute erreicht wurde
        if now.hour == hour and now.minute < minute:
            return False
        
        # Ab hier: Wir sind am richtigen Tag und der geplante Zeitpunkt ist erreicht oder überschritten
        
        # Prüfe ob heute schon gelaufen (oder innerhalb des Intervalls)
        cache_dir = os.path.join(SCRIPT_DIR, 'data', 'cache')
        cache_file = os.path.join(cache_dir, '.last_optimization_run')
        
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                last_run = datetime.fromtimestamp(int(f.read().strip()))
                
                # Wenn heute schon gelaufen, nicht nochmal
                if last_run.date() == now.date():
                    return False
                
                # Wenn innerhalb des Intervalls, nicht nochmal
                if (now - last_run).days < interval_days:
                    return False
        
        # Zeit für Optimierung!
        logging.info(f"🔄 Auto-Optimizer: Geplanter Zeitpunkt erreicht!")
        logging.info(f"    Geplant war: {['Mo','Di','Mi','Do','Fr','Sa','So'][day_of_week]} {hour:02d}:{minute:02d}")
        logging.info(f"    Starte Optimierung...")
        
        python_executable = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')
        optimizer_script = os.path.join(SCRIPT_DIR, 'auto_optimizer_scheduler.py')
        log_file = os.path.join(SCRIPT_DIR, 'logs', 'optimizer_output.log')
        
        if os.path.exists(optimizer_script):
            # Stelle sicher, dass logs/ Verzeichnis existiert
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            
            # Starte den Optimizer SYNCHRON (wartet auf Ende)
            # So wird die Telegram-Nachricht garantiert gesendet bevor wir weitermachen
            logging.info(f"    Starte Optimizer im Hintergrund...")
            with open(log_file, 'a') as log:
                # Starte als Hintergrundprozess - Bots haben Priorität!
                subprocess.Popen(
                    [python_executable, optimizer_script, '--force'],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=SCRIPT_DIR,  # Wichtig: Arbeitsverzeichnis setzen!
                    start_new_session=True  # Läuft unabhängig weiter
                )
            return True
        else:
            logging.error(f"Fehler: {optimizer_script} nicht gefunden!")
            return False
        
    except Exception as e:
        logging.error(f"Optimizer-Check Fehler: {e}")
        return False


def main():
    """
    Der Master Runner für den ltbbot (Envelope Strategie).
    - Liest die settings.json.
    - Startet für jede als "active" markierte Strategie einen separaten run.py Prozess.
    - Dieser Runner läuft EINMAL durch und wird durch den Cronjob regelmäßig neu aufgerufen.
    """
    settings_file = os.path.join(SCRIPT_DIR, 'settings.json')
    bot_runner_script = os.path.join(SCRIPT_DIR, 'src', 'ltbbot', 'strategy', 'run.py')
    secret_file = os.path.join(SCRIPT_DIR, 'secret.json')

    # Finde den Python-Interpreter in der venv
    python_executable = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')
    if not os.path.exists(python_executable):
        logging.critical(f"Fehler: Python-Interpreter in der venv nicht gefunden unter {python_executable}")
        return

    logging.info("=======================================================")
    logging.info("ltbbot Master Runner v1.1 (Cronjob-basiert)")
    logging.info("=======================================================")

    try:
        with open(settings_file, 'r') as f:
            settings = json.load(f)

        with open(secret_file, 'r') as f:
            secrets = json.load(f)

        if not secrets.get('ltbbot'): # Prüfe auf den richtigen Key
            logging.critical("Fehler: Kein 'ltbbot'-Account in secret.json gefunden.")
            return

        live_settings = settings.get('live_trading_settings', {})
        strategy_list = live_settings.get('active_strategies', [])

        if not strategy_list:
            logging.warning("Keine aktiven Strategien zum Ausführen gefunden.")
            return

        logging.info("=======================================================")

        # Überprüfe, welche Prozesse bereits laufen
        # (Einfache Prüfung; für robustere Implementierung wäre PID-Management nötig)
        # Für diese Architektur gehen wir davon aus, dass der Cron-Interval (z.B. 15min)
        # länger ist als die Ausführungszeit von run.py.
        # Wir starten einfach alle als aktiv markierten Prozesse.
        # Der Guardian im run.py verhindert den Start, falls etwas schiefgeht.

        for i, strategy_info in enumerate(strategy_list):
            if not isinstance(strategy_info, dict):
                logging.warning(f"Ungültiger Eintrag in active_strategies (kein Dictionary): {strategy_info}")
                continue

            if not strategy_info.get("active", False): # Prüfe, ob aktiv
                continue # Überspringe inaktive

            symbol = strategy_info.get('symbol')
            timeframe = strategy_info.get('timeframe')

            if not symbol or not timeframe:
                logging.warning(f"Unvollständige Strategie-Info: {strategy_info}. Überspringe.")
                continue

            # (HINWEIS: Um zu verhindern, dass ein Prozess gestartet wird,
            # der vom letzten Cronjob noch läuft, könnte man hier eine PID-Prüfung
            # oder eine Lock-Datei pro Strategie einbauen.
            # Für den Moment verlassen wir uns auf den Guardian im run.py)

            logging.info(f"\n--- Starte Bot für: {symbol} ({timeframe}) ---")

            command = [
                python_executable,
                bot_runner_script,
                "--symbol", symbol,
                "--timeframe", timeframe,
            ]

            try:
                # Starte den Prozess und lass ihn im Hintergrund laufen
                # Popen startet den Prozess und geht sofort weiter
                process = subprocess.Popen(command)
                logging.info(f"Prozess für {symbol}_{timeframe} gestartet (PID: {process.pid}).")
                time.sleep(2) # Kurze Pause zwischen Starts
            except Exception as e:
                logging.error(f"Fehler beim Starten des Prozesses für {symbol}_{timeframe}: {e}")

        # === SCHLEIFE ENTFERNT ===
        # Das Skript beendet sich hier, der Cronjob startet es neu.

    except FileNotFoundError as e:
        logging.critical(f"Fehler: Eine wichtige Datei wurde nicht gefunden: {e}")
    except json.JSONDecodeError as e:
        logging.critical(f"Fehler beim Lesen einer JSON-Datei (settings.json oder secret.json): {e}")
    except Exception as e:
        logging.critical(f"Ein unerwarteter Fehler im Master Runner ist aufgetreten: {e}", exc_info=True)

if __name__ == "__main__":
    # ZUERST: Normale Bot-Starts (Trades haben Priorität!)
    main()
    
    # DANACH: Auto-Optimizer Check (läuft im Hintergrund)
    check_and_run_optimizer()
