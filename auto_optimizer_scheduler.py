#!/usr/bin/env python3
"""
auto_optimizer_scheduler.py

Prueft bei jedem Aufruf ob eine Portfolio-Optimierung faellig ist und fuehrt
show_results.py --mode 3 --auto auf ALLEN vorhandenen Configs aus.
Configs werden manuell per run_pipeline.sh erstellt.

Aufruf:
  python3 auto_optimizer_scheduler.py           # normale Pruefung
  python3 auto_optimizer_scheduler.py --force   # sofort erzwingen
"""

import os
import sys
import json
import glob
import time
import subprocess
import argparse
from datetime import datetime

PROJECT_ROOT       = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

CACHE_DIR          = os.path.join(PROJECT_ROOT, 'data', 'cache')
LOG_DIR            = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE      = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE        = os.path.join(PROJECT_ROOT, 'secret.json')
SHOW_RESULTS_SCRIPT = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'analysis', 'show_results.py')
PORTFOLIO_RESULTS  = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'portfolio_optimization_results.json')
LAST_RUN_FILE      = os.path.join(CACHE_DIR, '.last_optimization_run')
IN_PROGRESS_FILE   = os.path.join(CACHE_DIR, '.optimization_in_progress')
TRIGGER_LOG        = os.path.join(LOG_DIR, 'auto_optimizer_trigger.log')


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{datetime.now().isoformat()} AUTO-PORTFOLIO {msg}"
    with open(TRIGGER_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _get_last_run() -> datetime | None:
    if not os.path.exists(LAST_RUN_FILE):
        return None
    with open(LAST_RUN_FILE, 'r') as f:
        s = f.read().strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _set_last_run():
    os.makedirs(CACHE_DIR, exist_ok=True)
    now_str = datetime.now().isoformat()
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(now_str)
    _log(f"LAST_RUN updated={now_str}")


def _is_due(schedule: dict) -> tuple[bool, str]:
    if os.path.exists(IN_PROGRESS_FILE):
        _log("SKIP already_in_progress")
        return False, None

    last_run = _get_last_run()
    if last_run is None:
        return True, 'forced'

    interval_cfg     = schedule.get('interval', {})
    value            = int(interval_cfg.get('value', 7))
    unit             = interval_cfg.get('unit', 'days')
    multipliers      = {'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}
    interval_seconds = value * multipliers.get(unit, 86400)

    if (datetime.now() - last_run).total_seconds() >= interval_seconds:
        return True, 'interval'

    now    = datetime.now()
    dow    = int(schedule.get('day_of_week', 0))
    hour   = int(schedule.get('hour', 3))
    minute = int(schedule.get('minute', 0))
    if now.weekday() == dow and now.hour == hour and minute <= now.minute < minute + 15:
        if last_run.date() < now.date():
            return True, 'scheduled'

    return False, None


def _scan_configs() -> list[dict]:
    """Liest alle vorhandenen Envelope-Configs und gibt Metadaten zurück."""
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    result = []
    for path in sorted(glob.glob(os.path.join(configs_dir, 'config_*_envelope.json'))):
        try:
            with open(path) as f:
                cfg = json.load(f)
            sym = cfg.get('market', {}).get('symbol', '')
            tf  = cfg.get('market', {}).get('timeframe', '')
            if sym and tf:
                result.append({'symbol': sym, 'timeframe': tf, 'path': path})
        except Exception:
            continue
    return result


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _get_telegram_credentials():
    try:
        with open(SECRET_FILE, 'r') as f:
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


def _send_start_telegram(configs: list, lookback_weeks: int, start_time: datetime):
    pair_list = ', '.join(f"{c['symbol'].split('/')[0]}/{c['timeframe']}" for c in configs)
    msg = (
        f"\U0001f4ca ltbbot Auto-Portfolio-Optimierung GESTARTET\n"
        f"Configs: {len(configs)}\n"
        f"Paare: {pair_list}\n"
        f"Lookback: {lookback_weeks} Wochen\n"
        f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    _send_telegram(msg)


def _send_end_telegram(elapsed: float):
    dur = _format_elapsed(elapsed)

    if not os.path.exists(PORTFOLIO_RESULTS):
        _send_telegram(f"✅ Auto-Portfolio-Optimierung abgeschlossen\nDauer: {dur}")
        return

    try:
        with open(PORTFOLIO_RESULTS, encoding='utf-8') as f:
            results = json.load(f)
        portfolio = results.get('optimal_portfolio', [])
        summary   = results.get('final_summary', {})
        pnl_pct   = summary.get('total_pnl_pct', 0)
        max_dd    = summary.get('max_drawdown_pct', 0)
        end_cap   = summary.get('end_capital', 0)
        sign      = '+' if pnl_pct >= 0 else ''

        portfolio_lines = '\n'.join(f"• {p}" for p in portfolio)
        msg = (
            f"✅ Auto-Portfolio-Optimierung abgeschlossen (Dauer: {dur})\n\n"
            f"Optimales Portfolio ({len(portfolio)} Strategie(n)):\n"
            f"{portfolio_lines}\n\n"
            f"PnL: {sign}{pnl_pct:.1f}%  |  MaxDD: {max_dd:.1f}%  |  End: {end_cap:.2f} USDT\n"
            f"✔ active_strategies in settings.json aktualisiert."
        )
        _send_telegram(msg)
    except Exception as e:
        _log(f"TELEGRAM_WARN cannot read portfolio results: {e}")
        _send_telegram(f"✅ Auto-Portfolio-Optimierung abgeschlossen\nDauer: {dur}")


# ---------------------------------------------------------------------------
# Haupt-Ablauf
# ---------------------------------------------------------------------------

def run_portfolio_optimization(opt_settings: dict, reason: str):
    os.makedirs(CACHE_DIR, exist_ok=True)

    configs = _scan_configs()
    if not configs:
        _log("SKIP no configs found — run run_pipeline.sh first")
        _send_telegram(
            "⚠️ ltbbot Auto-Portfolio-Optimierung: Keine Configs gefunden.\n"
            "Bitte zuerst run_pipeline.sh ausführen."
        )
        return

    lookback_weeks = int(opt_settings.get('backtest_lookback_weeks', 8))
    start_time     = datetime.now()

    _log(f"START reason={reason} configs={len(configs)} lookback_weeks={lookback_weeks}")

    with open(IN_PROGRESS_FILE, 'w') as f:
        f.write(start_time.isoformat())

    send_tg = opt_settings.get('send_telegram_on_completion', False)
    if send_tg:
        _send_start_telegram(configs, lookback_weeks, start_time)

    start_perf = time.time()
    success    = False

    try:
        cmd = [sys.executable, '-u', SHOW_RESULTS_SCRIPT, '--mode', '3', '--auto']
        _log(f"PORTFOLIO_OPTIMIZER_START cmd={' '.join(cmd)}")
        with open(TRIGGER_LOG, 'a', encoding='utf-8') as _lf:
            rc = subprocess.run(cmd, stdout=_lf, stderr=_lf).returncode
        _log(f"PORTFOLIO_OPTIMIZER_EXIT rc={rc}")
        success = (rc == 0)
    except Exception as e:
        _log(f"ERROR {e}")
    finally:
        if os.path.exists(IN_PROGRESS_FILE):
            os.remove(IN_PROGRESS_FILE)
            _log("IN_PROGRESS marker removed")

    elapsed = round(time.time() - start_perf, 1)

    if success:
        _set_last_run()
        _log(f"FINISH result=success elapsed_s={elapsed}")
        if send_tg:
            _send_end_telegram(elapsed)
    else:
        _log(f"FINISH result=failed elapsed_s={elapsed}")
        if send_tg:
            _send_telegram(
                f"❌ ltbbot Auto-Portfolio-Optimierung fehlgeschlagen\n"
                f"Dauer: {_format_elapsed(elapsed)}"
            )


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='ltbbot Auto-Portfolio-Optimierung Scheduler')
    parser.add_argument('--force', action='store_true',
                        help='Portfolio-Optimierung sofort erzwingen (ignoriert Zeitplan)')
    args = parser.parse_args()

    try:
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Fehler beim Lesen der settings.json: {e}")
        return

    opt_settings = settings.get('optimization_settings', {})

    if not opt_settings.get('enabled', False) and not args.force:
        _log("SKIP optimization disabled (enabled=false in settings.json)")
        return

    schedule = opt_settings.get('schedule', {
        'day_of_week': 4, 'hour': 15, 'minute': 0,
        'interval': {'value': 7, 'unit': 'days'},
    })

    if args.force:
        reason = 'forced'
    else:
        due, reason = _is_due(schedule)
        if not due:
            _log("SKIP not due yet (interval not reached)")
            return

    run_portfolio_optimization(opt_settings, reason)


if __name__ == '__main__':
    main()
