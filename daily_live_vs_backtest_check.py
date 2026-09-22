#!/usr/bin/env python3
"""
daily_live_vs_backtest_check.py  (ltbbot)

Vergleicht taeglich die echten Bitget-Live-Trades der letzten N Tage
(rollierendes Fenster, siehe live_vs_backtest_check_settings.rolling_window_days
in settings.json) mit einem fairen Backtest der AKTUELL aktiven Configs
(active_strategies) ueber denselben Zeitraum -- gleiche Methodik wie die
manuelle Analyse in [[research_ltbbot_live_vs_backtest_2026_09]]. Sendet das
Ergebnis einmal taeglich per Telegram.

Wird wie auto_optimizer_scheduler.py von master_runner.py bei jedem
15-Min-Zyklus im Hintergrund gestartet und prueft selbst, ob ein Lauf faellig
ist (einmal pro Tag, zur konfigurierten Stunde).

Aufruf:
  python3 daily_live_vs_backtest_check.py           # normale Pruefung
  python3 daily_live_vs_backtest_check.py --force   # sofort erzwingen, kein Telegram-Versand
  python3 daily_live_vs_backtest_check.py --force --send   # sofort erzwingen, MIT Telegram-Versand
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass  # z.B. wenn stdout umgeleitet ist -- auf Linux/VPS ohnehin UTF-8

from ltbbot.utils.exchange import Exchange
from ltbbot.analysis.backtester import load_data, run_envelope_backtest, FINE_TF_MAP, LazyFineData

CACHE_DIR      = os.path.join(PROJECT_ROOT, 'data', 'cache')
LOG_DIR        = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE  = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE    = os.path.join(PROJECT_ROOT, 'secret.json')
CONFIGS_DIR    = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
LAST_RUN_FILE  = os.path.join(CACHE_DIR, '.last_live_vs_backtest_check')
TRIGGER_LOG    = os.path.join(LOG_DIR, 'daily_live_vs_backtest_check.log')

DEFAULT_SETTINGS = {
    'enabled': True,
    'rolling_window_days': 30,
    'send_hour': 8,
    'start_capital_per_strategy': 50,
}


def _log(msg: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{datetime.now().isoformat()} LIVE-VS-BACKTEST {msg}"
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


def _is_due(send_hour: int) -> bool:
    last_run = _get_last_run()
    now = datetime.now()
    if now.hour != send_hour:
        return False
    if last_run is not None and last_run.date() >= now.date():
        return False
    return True


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


def _get_warmup_start_date(start_date_str: str, timeframe: str) -> str:
    tf_to_hours = {
        '1m': 1 / 60, '5m': 5 / 60, '15m': 0.25, '30m': 0.5,
        '1h': 1, '2h': 2, '4h': 4, '6h': 6, '8h': 8, '12h': 12, '1d': 24
    }
    hours = tf_to_hours.get(timeframe, 24)
    warmup_days = max(int((300 * hours) / 24) + 1, 14)
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    return (start_dt - timedelta(days=warmup_days)).strftime("%Y-%m-%d")


def _load_active_strategies() -> list:
    with open(SETTINGS_FILE) as f:
        settings = json.load(f)
    strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    return [s for s in strategies if s.get('active', True) and s.get('symbol') and s.get('timeframe')]


def _config_path_for(symbol: str, timeframe: str) -> str | None:
    base = symbol.replace('/', '').replace(':', '').upper()
    candidate = os.path.join(CONFIGS_DIR, f"config_{base}_{timeframe}_envelope.json")
    return candidate if os.path.exists(candidate) else None


def run_backtest_side(strategies: list, start_date: str, end_date: str, start_capital: float) -> dict:
    """Faire Backtest-Seite: fuer jede AKTUELL aktive Strategie ueber das
    rollierende Fenster, identische Methodik wie run_fair_backtest.py aus
    der manuellen Analyse."""
    per_symbol = {}
    for strat in strategies:
        symbol, timeframe = strat['symbol'], strat['timeframe']
        cfg_path = _config_path_for(symbol, timeframe)
        base = symbol.split('/')[0]
        if not cfg_path:
            _log(f"WARN keine Config gefunden fuer {symbol} ({timeframe})")
            continue
        try:
            with open(cfg_path) as f:
                config = json.load(f)
            warmup_start = _get_warmup_start_date(start_date, timeframe)
            data = load_data(symbol, timeframe, warmup_start, end_date)
            if data is None or data.empty:
                continue
            fine_tf = FINE_TF_MAP.get(timeframe)
            fine_data = LazyFineData(symbol, fine_tf) if fine_tf else None
            result = run_envelope_backtest(data.copy(), config, start_capital, show_progress=False,
                                            sim_start_date=start_date, fine_data=fine_data,
                                            multi_band_entries=True)
            per_symbol[base] = {
                'trades': result.get('trades_count', 0),
                'win_rate': result.get('win_rate', 0),
                'pnl_usd': result.get('end_capital', start_capital) - start_capital,
            }
        except Exception as e:
            _log(f"WARN Backtest fehlgeschlagen fuer {symbol} ({timeframe}): {e}")
    return per_symbol


def run_live_side(strategies: list, since_ms: int) -> dict:
    with open(SECRET_FILE) as f:
        secret = json.load(f)
    acc = secret['ltbbot'][0]
    exchange = Exchange(acc)

    active_bases = {s['symbol'].split('/')[0] for s in strategies}
    positions = exchange.fetch_closed_positions_history(since_ms)

    per_symbol = {}
    for p in positions:
        base = p['symbol'].replace('USDT', '')
        if base not in active_bases:
            continue  # nur Symbole, die AKTUELL im Portfolio sind
        entry = per_symbol.setdefault(base, {'trades': 0, 'wins': 0, 'pnl_usd': 0.0})
        entry['trades'] += 1
        if float(p.get('pnl', 0)) > 0:
            entry['wins'] += 1
        entry['pnl_usd'] += float(p.get('netProfit', 0))

    for base, entry in per_symbol.items():
        entry['win_rate'] = (entry['wins'] / entry['trades'] * 100) if entry['trades'] else 0.0

    return per_symbol


def build_message(live: dict, backtest: dict, window_days: int) -> str:
    all_symbols = sorted(set(live.keys()) | set(backtest.keys()))

    live_trades = sum(v['trades'] for v in live.values())
    live_wins = sum(v['wins'] for v in live.values())
    live_pnl = sum(v['pnl_usd'] for v in live.values())
    live_wr = (live_wins / live_trades * 100) if live_trades else 0.0

    bt_trades = sum(v['trades'] for v in backtest.values())
    bt_pnl = sum(v['pnl_usd'] for v in backtest.values())
    bt_wins_approx = sum(v['trades'] * v['win_rate'] / 100 for v in backtest.values())
    bt_wr = (bt_wins_approx / bt_trades * 100) if bt_trades else 0.0

    lines = [
        f"📊 ltbbot Live vs. Backtest (letzte {window_days} Tage)",
        "",
        f"Live:      {live_trades} Trades | WR {live_wr:.1f}% | PnL {live_pnl:+.2f} USDT",
        f"Backtest:  {bt_trades} Trades | WR {bt_wr:.1f}% | PnL {bt_pnl:+.2f} USDT",
        "",
    ]

    # Groesste Abweichungen: Symbole mit Live-Trades, deren WR am staerksten
    # vom Backtest abweicht (Betrag), max. 5 Zeilen.
    divergences = []
    for sym in all_symbols:
        l = live.get(sym, {'trades': 0, 'win_rate': 0.0, 'pnl_usd': 0.0})
        b = backtest.get(sym, {'trades': 0, 'win_rate': 0.0, 'pnl_usd': 0.0})
        if l['trades'] == 0:
            continue
        wr_gap = abs(l['win_rate'] - b['win_rate'])
        divergences.append((wr_gap, sym, l, b))
    divergences.sort(key=lambda x: -x[0])

    if divergences:
        lines.append("Groesste Abweichungen (WR):")
        for _, sym, l, b in divergences[:5]:
            lines.append(
                f"• {sym}: Live {l['win_rate']:.0f}% ({l['trades']}T, {l['pnl_usd']:+.2f}$) "
                f"| BT {b['win_rate']:.0f}% ({b['trades']}T, {b['pnl_usd']:+.2f}$)"
            )

    return "\n".join(lines)


def run_check(window_days: int, send_hour: int, start_capital: float, send: bool):
    start_perf = time.time()
    now = datetime.now(timezone.utc)
    since_ms = int((now - timedelta(days=window_days)).timestamp() * 1000)
    start_date = (now - timedelta(days=window_days)).strftime('%Y-%m-%d')
    end_date = now.strftime('%Y-%m-%d')

    strategies = _load_active_strategies()
    if not strategies:
        _log("SKIP keine active_strategies in settings.json")
        return

    _log(f"START window_days={window_days} strategies={len(strategies)}")

    try:
        live = run_live_side(strategies, since_ms)
        backtest = run_backtest_side(strategies, start_date, end_date, start_capital)
    except Exception as e:
        _log(f"ERROR {e}")
        if send:
            _send_telegram(f"❌ ltbbot Live-vs-Backtest-Check fehlgeschlagen: {e}")
        return

    message = build_message(live, backtest, window_days)
    elapsed = round(time.time() - start_perf, 1)
    _log(f"FINISH elapsed_s={elapsed}\n{message}")

    if send:
        _send_telegram(message)
    else:
        print("\n--- Telegram-Nachricht (NICHT gesendet, --send fehlt) ---")
        print(message)


def main():
    parser = argparse.ArgumentParser(description='ltbbot Live-vs-Backtest Daily Check')
    parser.add_argument('--force', action='store_true', help='Sofort ausfuehren, ignoriert Zeitplan')
    parser.add_argument('--send', action='store_true', help='Telegram-Nachricht wirklich senden')
    args = parser.parse_args()

    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Fehler beim Lesen der settings.json: {e}")
        return

    cfg = {**DEFAULT_SETTINGS, **settings.get('live_vs_backtest_check_settings', {})}

    if not cfg['enabled'] and not args.force:
        _log("SKIP disabled (live_vs_backtest_check_settings.enabled=false)")
        return

    if not args.force and not _is_due(int(cfg['send_hour'])):
        return

    send = args.send or (args.force is False)  # regulaerer (nicht --force) Lauf sendet immer
    run_check(int(cfg['rolling_window_days']), int(cfg['send_hour']), float(cfg['start_capital_per_strategy']), send)
    if not args.force:
        _set_last_run()


if __name__ == '__main__':
    main()
