#!/usr/bin/env python3
"""
auto_parameter_optimizer_scheduler.py  (ltbbot)

Schritt 1 der Optimizer-Pipeline (Envelope-Parameter-Suche pro Symbol/
Timeframe, optimizer.py) -- getrennt von auto_optimizer_scheduler.py, das nur
Schritt 2 (woechentliche Portfolio-AUSWAHL aus bestehenden Configs,
portfolio_optimizer.py) automatisiert. Ohne dieses Skript liefe Schritt 1 nie
automatisch: die Configs (Envelopes, SL, Leverage) frieren dann auf dem Stand
der letzten manuellen run_pipeline.sh-Ausfuehrung ein, waehrend Schritt 2
jede Woche nur unter denselben, zunehmend veralteten Parametern auswaehlt
(siehe research_ltbbot_live_vs_backtest_2026_09, 2026-09-25 -- Median
OOS/IS-PnL-Verhaeltnis lag bei 0.10 trotz woechentlich aktivem Schritt 2).

Bewusst NICHT "alle Configs auf einmal" (ein voller Lauf ueber alle Symbole/
Timeframes braucht mehrere Stunden, siehe der manuelle Lauf vom 2026-09-25) --
stattdessen wird bei jeder Faelligkeit NUR das EINE Symbol/Timeframe-Paar neu
optimiert, dessen Config am laengsten nicht mehr geprueft wurde (optimizer.py
selbst nutzt zusaetzlich --recheck-after-days als zweite Sperre pro Paar).
Das verteilt die Rechenlast ueber viele Tage, blockiert nie einen ganzen
15-Minuten-Cronzyklus mehrfach hintereinander, und deckt ueber die Zeit den
gesamten Config-Pool ab (recheck_after_days bestimmt den vollen Umlauf).

Aufruf:
  python3 auto_parameter_optimizer_scheduler.py           # normale Pruefung
  python3 auto_parameter_optimizer_scheduler.py --force   # sofort erzwingen (ein Paar)
"""
import os
import sys
import json
import glob
import time
import subprocess
import argparse
from datetime import datetime, timedelta

PROJECT_ROOT     = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

CACHE_DIR        = os.path.join(PROJECT_ROOT, 'data', 'cache')
LOG_DIR          = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE    = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE      = os.path.join(PROJECT_ROOT, 'secret.json')
CONFIGS_DIR      = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
OPTIMIZER_SCRIPT = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'analysis', 'optimizer.py')
LAST_RUN_FILE    = os.path.join(CACHE_DIR, '.last_parameter_search_run')
IN_PROGRESS_FILE = os.path.join(CACHE_DIR, '.parameter_search_in_progress')
TRIGGER_LOG      = os.path.join(LOG_DIR, 'auto_parameter_optimizer_trigger.log')


def _log(msg: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{datetime.now().isoformat()} PARAM-SEARCH {msg}"
    with open(TRIGGER_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass


def _get_last_run():
    if not os.path.exists(LAST_RUN_FILE):
        return None
    try:
        with open(LAST_RUN_FILE) as f:
            return datetime.fromisoformat(f.read().strip())
    except ValueError:
        return None


def _set_last_run():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(datetime.now().isoformat())


def _is_due() -> bool:
    if os.path.exists(IN_PROGRESS_FILE):
        _log("SKIP already_in_progress")
        return False
    last_run = _get_last_run()
    if last_run is None:
        return True
    return (datetime.now() - last_run) >= timedelta(days=1)


def _get_telegram_credentials():
    try:
        with open(SECRET_FILE) as f:
            secrets = json.load(f)
        tg = secrets.get('telegram', {})
        return tg.get('bot_token'), tg.get('chat_id')
    except Exception:
        return None, None


def _send_telegram(message: str):
    bot_token, chat_id = _get_telegram_credentials()
    if not bot_token or not chat_id:
        _log("TELEGRAM SKIP kein token/chat_id in secret.json")
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={'chat_id': chat_id, 'text': message},
            timeout=10
        )
        _log("TELEGRAM sent")
    except Exception as e:
        _log(f"TELEGRAM ERROR {e}")


def _pick_stalest_pair(config_suffix: str):
    """Findet unter allen vorhandenen Config-Dateien diejenige mit dem
    aeltesten (oder fehlenden) _meta.optimized_at -- das Symbol/Timeframe-
    Paar, das am dringendsten eine Neupruefung braucht."""
    pattern = os.path.join(CONFIGS_DIR, f'config_*{config_suffix}.json')
    candidates = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        symbol = cfg.get('market', {}).get('symbol', '')
        timeframe = cfg.get('market', {}).get('timeframe', '')
        if not symbol or not timeframe:
            continue
        optimized_at_str = cfg.get('_meta', {}).get('optimized_at')
        try:
            optimized_at = datetime.fromisoformat(optimized_at_str) if optimized_at_str else datetime.min
        except ValueError:
            optimized_at = datetime.min
        candidates.append((optimized_at, symbol, timeframe, path))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0]  # aeltestes zuerst


def run_parameter_search(opt_settings: dict, search_settings: dict):
    os.makedirs(CACHE_DIR, exist_ok=True)
    config_suffix = opt_settings.get('config_suffix', '_envelope')

    stalest = _pick_stalest_pair(config_suffix)
    if not stalest:
        _log("SKIP keine Config-Dateien gefunden -- run_pipeline.sh muss zuerst mind. einmal manuell laufen")
        return
    optimized_at, symbol, timeframe, path = stalest
    base_symbol = symbol.split('/')[0]

    recheck_after_days = int(search_settings.get('recheck_after_days', 30))
    age_days = (datetime.now() - optimized_at).days if optimized_at != datetime.min else 999999
    if age_days < recheck_after_days:
        _log(f"SKIP {base_symbol} ({timeframe}) erst vor {age_days}d optimiert (Sperre: {recheck_after_days}d) -- "
             f"gesamter Pool bereits aktuell, nichts faellig")
        return

    start_time = datetime.now()
    _log(f"START {base_symbol} ({timeframe}) zuletzt optimiert: {optimized_at if optimized_at != datetime.min else 'nie'}")

    with open(IN_PROGRESS_FILE, 'w') as f:
        f.write(f"{base_symbol} {timeframe} {start_time.isoformat()}")

    send_tg = search_settings.get('send_telegram_on_completion', True)
    if send_tg:
        _send_telegram(f"\U0001f9ea ltbbot Parameter-Suche gestartet: {base_symbol} ({timeframe})\n"
                        f"(zuletzt optimiert: {optimized_at.date() if optimized_at != datetime.min else 'nie'})")

    lookback_weeks = int(opt_settings.get('backtest_lookback_weeks', 26))
    end_date = start_time.strftime('%Y-%m-%d')
    search_start_date = (start_time - timedelta(weeks=lookback_weeks)).strftime('%Y-%m-%d')

    cmd = [
        sys.executable, '-u', OPTIMIZER_SCRIPT,
        '--symbols', base_symbol,
        '--timeframes', timeframe,
        '--start_date', search_start_date,
        '--end_date', end_date,
        '--jobs', '1',
        '--max_drawdown', str(opt_settings.get('constraints', {}).get('max_drawdown_pct', 30)),
        '--start_capital', str(opt_settings.get('start_capital', 10)),
        '--min_win_rate', str(opt_settings.get('constraints', {}).get('min_win_rate_pct', 0)),
        '--trials', str(opt_settings.get('num_trials', 500)),
        '--min_pnl', str(opt_settings.get('constraints', {}).get('min_pnl_pct', 0)),
        '--mode', opt_settings.get('mode', 'strict'),
        '--min_trades_per_year', '20',
        '--is_fraction', str(opt_settings.get('is_fraction', 0.7)),
        '--k_folds', str(opt_settings.get('k_folds', 3)),
        '--min_oos_trades', str(opt_settings.get('min_oos_trades', 10)),
        '--min_oos_profit_factor', str(opt_settings.get('min_oos_profit_factor', 1.3)),
        '--config_suffix', config_suffix,
        '--recheck-confirmed',
    ]

    success = False
    try:
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        with open(TRIGGER_LOG, 'a', encoding='utf-8') as _lf:
            rc = subprocess.run(cmd, stdout=_lf, stderr=_lf, env=env).returncode
        success = (rc == 0)
    except Exception as e:
        _log(f"ERROR {e}")
    finally:
        if os.path.exists(IN_PROGRESS_FILE):
            os.remove(IN_PROGRESS_FILE)

    elapsed_min = (datetime.now() - start_time).total_seconds() / 60

    # Ergebnis aus der (jetzt evtl. aktualisierten) Config auslesen, fuer die Telegram-Meldung
    result_summary = ""
    try:
        with open(path) as f:
            new_cfg = json.load(f)
        new_meta = new_cfg.get('_meta', {})
        if new_meta.get('optimized_at') and new_meta['optimized_at'] > optimized_at.isoformat():
            result_summary = (f"✅ Bestaetigt -- neue Config gespeichert.\n"
                              f"IS PnL: {new_meta.get('pnl_pct', 0):+.1f}% | "
                              f"OOS PnL: {new_meta.get('oos_pnl_pct', 0):+.1f}% | "
                              f"OOS Profit-Faktor: {new_meta.get('oos_profit_factor', 0):.2f} | "
                              f"OOS WR: {new_meta.get('oos_win_rate', 0):.1f}%")
        else:
            result_summary = "⏭ Nicht bestaetigt -- alte Config bleibt aktiv (siehe Log fuer Details)."
    except Exception:
        result_summary = "(Ergebnis konnte nicht ausgelesen werden, siehe Log)"

    _log(f"FINISH {base_symbol} ({timeframe}) result={'success' if success else 'failed'} elapsed_min={elapsed_min:.1f}")

    if send_tg:
        _send_telegram(
            f"\U0001f9ea ltbbot Parameter-Suche abgeschlossen: {base_symbol} ({timeframe})\n"
            f"Dauer: {elapsed_min:.0f} Min\n{result_summary}"
        )

    if success:
        _set_last_run()


def main():
    parser = argparse.ArgumentParser(description='ltbbot Parameter-Suche Scheduler (Schritt 1)')
    parser.add_argument('--force', action='store_true', help='Sofort ein Paar optimieren, ignoriert Zeitplan')
    args = parser.parse_args()

    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Fehler beim Lesen der settings.json: {e}")
        return

    search_settings = settings.get('parameter_search_settings', {})
    if not search_settings.get('enabled', False) and not args.force:
        _log("SKIP disabled (parameter_search_settings.enabled=false)")
        return

    if not args.force and not _is_due():
        return

    opt_settings = settings.get('optimization_settings', {})
    run_parameter_search(opt_settings, search_settings)


if __name__ == '__main__':
    main()
