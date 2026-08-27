#!/usr/bin/env python3
"""
run_28pair_sweep.py

Treibt einen 28-Kombinationen-Optimierungslauf (BTC/XRP/ETH/SOL/ADA/AAVE/DOGE
x 6h/4h/2h/1h) sequenziell durch optimizer.py -- fuer eine komplette
Neu-Optimierung aller Symbol/Timeframe-Paare aus dem Stand (z.B. nach
`rm -f src/ltbbot/strategy/configs/config_*.json`).

Pro Paar: erst 200 Trials. Liefert das `no_valid_trials` (Optuna fand keinen
gueltigen Trial unter den Constraints), automatisch ein zweiter Versuch mit
600 Trials. Jeder Subprozess-Aufruf wird komplett geloggt; Ergebnis pro Paar
wird aus (a) dem eigenen stdout-Log (fuer WinRate, die NICHT in
last_optimizer_run.json landet) und (b) artifacts/results/last_optimizer_run.json
(fuer den offiziellen 'confirmed'-Status inkl. Baseline-Vergleich) zusammengefuehrt.

Nutzt die Produktionsparameter (Live-konsistent: IS/OOS-Split + multi_band_entries
+ Compounding sind seit 2026-08-27 Standardverhalten in optimizer.py):
  --start_capital 50 --jobs -1 --max_drawdown 30 --min_win_rate 0 --min_pnl 0
  --mode strict --min_trades_per_year 20 --config_suffix _envelope
  --is_fraction 0.70 --k_folds 3 --min_oos_trades 10
  --recheck-confirmed --recheck-after-days 0
(Die letzten beiden Flags umgehen die Re-Optimierungs-Sperre bewusst -- dieser
Sweep IST die gewollte volle Neubewertung, siehe optimizer.py-Docstring der
Sperre. Fuer den NORMALEN woechentlichen Betrieb NICHT diese Flags verwenden,
sonst wird staendig unnoetig neu optimiert.)

Lookback je Timeframe (identisch zu run_pipeline.sh):
  6h/4h -> 1095 Tage | 2h -> 730 Tage | 1h -> 548 Tage

Aufruf (venv wird automatisch erkannt: .venv, dann .venv_new als Fallback):
  python3 run_28pair_sweep.py
Resume-faehig: bereits abgeschlossene Paare (in sweep_28pairs_results.json
als "final": true markiert) werden bei erneutem Start uebersprungen.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _find_python():
    for venv_name in ('.venv', '.venv_new'):
        candidate = os.path.join(PROJECT_ROOT, venv_name, 'bin', 'python')
        if os.path.exists(candidate):
            return candidate
    return sys.executable


PYTHON = _find_python()
OPTIMIZER = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'analysis', 'optimizer.py')
RESULTS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_optimizer_run.json')
OUT_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'sweep_28pairs')
LOG_DIR = os.path.join(OUT_DIR, 'logs')
SUMMARY_JSON = os.path.join(OUT_DIR, 'sweep_28pairs_results.json')
os.makedirs(LOG_DIR, exist_ok=True)

SYMBOLS = ['BTC', 'XRP', 'ETH', 'SOL', 'ADA', 'AAVE', 'DOGE']
TIMEFRAMES = ['6h', '4h', '2h', '1h']
LOOKBACK_DAYS = {'6h': 1095, '4h': 1095, '2h': 730, '1h': 548}

END_DATE = date.today().strftime('%Y-%m-%d')

COMMON_ARGS = [
    '--jobs', '-1',
    '--max_drawdown', '30',
    '--start_capital', '50',
    '--min_win_rate', '0',
    '--min_pnl', '0',
    '--mode', 'strict',
    '--min_trades_per_year', '20',
    '--config_suffix', '_envelope',
    '--is_fraction', '0.70',
    '--k_folds', '3',
    '--min_oos_trades', '10',
    '--recheck-confirmed',
    '--recheck-after-days', '0',
]

WR_RE = re.compile(r'OOS PnL:\s*([+-]?[\d.]+)%\s*\|\s*DD:\s*([+-]?[\d.]+)%\s*\|\s*Trades:\s*(\d+)\s*\|\s*WR:\s*([\d.]+)%')
IS_RE = re.compile(r'IS\s+PnL:\s*([+-]?[\d.]+)%\s*\|\s*DD:\s*([+-]?[\d.]+)%\s*\|\s*Trades:\s*(\d+)\s*\|\s*WR:\s*([\d.]+)%')
NO_VALID_RE = re.compile(r'konnte keine g.ltige Konfiguration gefunden werden|no_valid_trials')


def _load_results_file():
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {'saved': [], 'skipped': [], 'failed': []}
    return {'saved': [], 'skipped': [], 'failed': []}


def _find_entry(run_results, symbol_full, timeframe):
    for category in ('saved', 'skipped', 'failed'):
        for entry in reversed(run_results.get(category, [])):
            if entry.get('symbol') == symbol_full and entry.get('timeframe') == timeframe:
                return category, entry
    return None, None


def run_one(symbol, timeframe, trials, attempt_label):
    symbol_full = f"{symbol.upper()}/USDT:USDT"
    lookback = LOOKBACK_DAYS[timeframe]
    start_date = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')

    cmd = [PYTHON, OPTIMIZER,
           '--symbols', symbol, '--timeframes', timeframe,
           '--start_date', start_date, '--end_date', END_DATE,
           '--trials', str(trials)] + COMMON_ARGS

    log_path = os.path.join(LOG_DIR, f"{symbol}_{timeframe}_{attempt_label}.log")
    print(f"\n{'='*70}\n>>> {symbol} ({timeframe}) -- {trials} Trials -- {start_date} -> {END_DATE}\n{'='*70}", flush=True)
    t0 = time.time()
    with open(log_path, 'w', encoding='utf-8') as logf:
        proc = subprocess.run(cmd, cwd=PROJECT_ROOT, stdout=logf, stderr=subprocess.STDOUT)
    elapsed = time.time() - t0
    print(f"    -> exit={proc.returncode}  dauer={elapsed/60:.1f}min  log={log_path}", flush=True)

    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        log_text = f.read()

    no_valid = bool(NO_VALID_RE.search(log_text))

    run_results = _load_results_file()
    category, entry = _find_entry(run_results, symbol_full, timeframe)

    wr_match = WR_RE.search(log_text)
    is_match = IS_RE.search(log_text)

    parsed = {
        'symbol': symbol, 'timeframe': timeframe, 'trials_used': trials,
        'attempt': attempt_label, 'elapsed_min': round(elapsed / 60, 1),
        'exit_code': proc.returncode, 'log_file': os.path.relpath(log_path, PROJECT_ROOT),
        'no_valid_trials': no_valid, 'category': category,
    }
    if entry:
        parsed.update({f'entry_{k}': v for k, v in entry.items()})
    if wr_match:
        parsed['oos_pnl_pct_parsed'] = float(wr_match.group(1))
        parsed['oos_dd_pct_parsed'] = float(wr_match.group(2))
        parsed['oos_trades_parsed'] = int(wr_match.group(3))
        parsed['oos_win_rate_parsed'] = float(wr_match.group(4))
    if is_match:
        parsed['is_pnl_pct_parsed'] = float(is_match.group(1))
        parsed['is_dd_pct_parsed'] = float(is_match.group(2))
        parsed['is_trades_parsed'] = int(is_match.group(3))
        parsed['is_win_rate_parsed'] = float(is_match.group(4))

    return parsed


def main():
    print(f"Python: {PYTHON}")
    print(f"Output: {OUT_DIR}\n")
    all_results = []
    if os.path.exists(SUMMARY_JSON):
        try:
            with open(SUMMARY_JSON) as f:
                all_results = json.load(f)
        except Exception:
            all_results = []
    done_pairs = {(r['symbol'], r['timeframe']) for r in all_results if r.get('final')}

    total = len(SYMBOLS) * len(TIMEFRAMES)
    idx = 0
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            idx += 1
            if (symbol, timeframe) in done_pairs:
                print(f"[{idx}/{total}] {symbol} ({timeframe}) -- bereits abgeschlossen, ueberspringe (Resume).", flush=True)
                continue
            print(f"\n########## [{idx}/{total}] {symbol} / {timeframe} ##########", flush=True)

            result_200 = run_one(symbol, timeframe, 200, 'trials200')
            all_results.append(result_200)
            with open(SUMMARY_JSON, 'w', encoding='utf-8') as f:
                json.dump(all_results, f, indent=2)

            final_result = result_200
            if result_200['no_valid_trials'] or result_200['category'] == 'failed':
                print(f"    {symbol} ({timeframe}): 200 Trials -> no_valid_trials/failed. Retry mit 600 Trials...", flush=True)
                result_600 = run_one(symbol, timeframe, 600, 'trials600')
                all_results.append(result_600)
                final_result = result_600

            final_result = dict(final_result)
            final_result['final'] = True
            all_results.append(final_result)
            with open(SUMMARY_JSON, 'w', encoding='utf-8') as f:
                json.dump(all_results, f, indent=2)

    print("\n\n===== 28-PAAR-SWEEP ABGESCHLOSSEN =====", flush=True)
    finals = [r for r in all_results if r.get('final')]
    for r in finals:
        conf = r.get('entry_confirmed', False)
        mark = 'BESTAETIGT' if conf else 'nicht bestaetigt'
        print(f"  {r['symbol']:>5} {r['timeframe']:>3}  trials={r['trials_used']:<3}  {mark}", flush=True)


if __name__ == '__main__':
    main()
