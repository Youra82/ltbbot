#!/usr/bin/env python3
"""
auto_portfolio_scheduler.py  (ltbbot)

Prueft bei jedem Aufruf ob eine Portfolio-Optimierung faellig ist und
fuehrt run_portfolio_optimizer.py --auto-write aus.
Sendet Telegram-Benachrichtigungen bei Start und Ende.

Aufruf:
  python3 auto_portfolio_scheduler.py           # normale Pruefung
  python3 auto_portfolio_scheduler.py --force   # sofort erzwingen
"""
import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime

PROJECT_ROOT     = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR        = os.path.join(PROJECT_ROOT, 'data', 'cache')
LOG_DIR          = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE    = os.path.join(PROJECT_ROOT, 'settings.json')
PORTFOLIO_SCRIPT = os.path.join(PROJECT_ROOT, 'run_portfolio_optimizer.py')
SECRET_FILE      = os.path.join(PROJECT_ROOT, 'secret.json')
_ARTIFACTS_DIR   = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
LAST_RUN_FILE    = os.path.join(_ARTIFACTS_DIR, '.last_portfolio_opt_run')
IN_PROGRESS_FILE = os.path.join(_ARTIFACTS_DIR, '.portfolio_opt_in_progress')
TRIGGER_LOG      = os.path.join(LOG_DIR, 'auto_portfolio_trigger.log')


def _log(msg: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{datetime.now().isoformat()} PORTFOLIO-OPT {msg}"
    with open(TRIGGER_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass


def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _get_last_run():
    if not os.path.exists(LAST_RUN_FILE):
        return None
    with open(LAST_RUN_FILE) as f:
        s = f.read().strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _set_last_run():
    os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
    now_str = datetime.now().isoformat()
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(now_str)
    _log(f"LAST_RUN updated={now_str}")


def _is_due(schedule: dict) -> tuple:
    if os.path.exists(IN_PROGRESS_FILE):
        _log("SKIP already_in_progress")
        return False, None

    last_run = _get_last_run()
    if last_run is None:
        return True, 'first_run'

    interval_cfg     = schedule.get('interval', {})
    value            = int(interval_cfg.get('value', 7))
    unit             = interval_cfg.get('unit', 'days')
    multipliers      = {'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}
    interval_seconds = value * multipliers.get(unit, 86400)

    if (datetime.now() - last_run).total_seconds() >= interval_seconds:
        return True, 'interval'

    now    = datetime.now()
    dow    = int(schedule.get('day_of_week', 5))
    hour   = int(schedule.get('hour', 15))
    minute = int(schedule.get('minute', 0))
    if now.weekday() == dow and now.hour == hour and minute <= now.minute < minute + 15:
        if last_run.date() < now.date():
            return True, 'scheduled'

    return False, None


def _get_telegram_credentials():
    try:
        with open(SECRET_FILE) as f:
            secrets = json.load(f)
        tg = secrets.get('telegram', {})
        return tg.get('bot_token'), tg.get('chat_id')
    except Exception:
        return None, None


def _send_telegram_plain(message: str):
    bot_token, chat_id = _get_telegram_credentials()
    if not bot_token or not chat_id:
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                      data={'chat_id': chat_id, 'text': message}, timeout=10)
        _log("TELEGRAM sent")
    except Exception as e:
        _log(f"TELEGRAM ERROR {e}")


def _send_end_telegram(elapsed_seconds: float):
    dur = _format_elapsed(elapsed_seconds)
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            settings = json.load(f)
        strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
        active     = [s for s in strategies if s.get('active')]
        lines      = [f"ltbbot Portfolio-Optimizer abgeschlossen (Dauer: {dur})"]
        if active:
            lines.append(f"\nAktives Portfolio ({len(active)} Strategie(n)):")
            for s in active:
                sym_short = s['symbol'].split('/')[0]
                lines.append(f"* {sym_short}/{s['timeframe']}")
        else:
            lines.append("\nKein Portfolio eingetragen.")
        _send_telegram_plain('\n'.join(lines))
    except Exception:
        _send_telegram_plain(f"ltbbot Portfolio-Optimizer abgeschlossen (Dauer: {dur})")


def run_optimization(opt_settings: dict, reason: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    start_time = datetime.now()
    send_tg    = opt_settings.get('send_telegram_on_completion', False)

    _log(f"START reason={reason}")

    os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
    with open(IN_PROGRESS_FILE, 'w') as f:
        f.write(start_time.isoformat())

    # Timestamp sofort schreiben — verhindert Re-Trigger auch bei Fehlschlag
    _set_last_run()

    if send_tg:
        _send_telegram_plain(
            f"ltbbot Portfolio-Optimizer GESTARTET\n"
            f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Fuehrt Backtests aller Envelope-Configs durch und waehlt bestes Portfolio."
        )

    start_perf = time.time()
    success    = False

    try:
        capital = str(opt_settings.get('start_capital', 50))
        max_dd  = str(opt_settings.get('constraints', {}).get('max_drawdown_pct', 30))
        cmd     = [sys.executable, PORTFOLIO_SCRIPT,
                   '--capital', capital, '--max-dd', max_dd, '--auto-write']
        _log(f"CMD capital={capital} max_dd={max_dd}")
        result  = subprocess.run(cmd)
        success = (result.returncode == 0)
        _log(f"EXIT rc={result.returncode}")
    except Exception as e:
        _log(f"ERROR {e}")
    finally:
        if os.path.exists(IN_PROGRESS_FILE):
            os.remove(IN_PROGRESS_FILE)

    elapsed = round(time.time() - start_perf, 1)

    if success:
        _log(f"FINISH result=success elapsed_s={elapsed}")
        if send_tg:
            _send_end_telegram(elapsed)
    else:
        _log(f"FINISH result=failed elapsed_s={elapsed}")


def main():
    parser = argparse.ArgumentParser(description='ltbbot Portfolio-Optimizer Scheduler')
    parser.add_argument('--force', action='store_true',
                        help='Optimierung sofort erzwingen (ignoriert Zeitplan)')
    args = parser.parse_args()

    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Fehler beim Lesen der settings.json: {e}")
        return

    opt_settings = settings.get('optimization_settings', {})

    if not opt_settings.get('enabled', False) and not args.force:
        print("Portfolio-Optimierung deaktiviert (optimization_settings.enabled=false).")
        return

    schedule = opt_settings.get('schedule', {
        'day_of_week': 5, 'hour': 15, 'minute': 0,
        'interval': {'value': 7, 'unit': 'days'},
    })

    if args.force:
        reason = 'forced'
    else:
        due, reason = _is_due(schedule)
        if not due:
            print("Portfolio-Optimierung noch nicht faellig.")
            return

    run_optimization(opt_settings, reason)


if __name__ == '__main__':
    main()
