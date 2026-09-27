#!/usr/bin/env python3
"""
sync_confirmed_flags.py

Bewertet JEDE bestehende Config (config_*_envelope.json) mit ihren aktuellen
Parametern neu gegen die OOS-Bestaetigung des Optimizers und schreibt das
Ergebnis in _meta (confirmed, OOS-Kennzahlen, rechecked_at). Parameter bleiben
unveraendert.

Warum (2026-09-27): _meta.confirmed stand bisher fuer immer auf dem Wert des
Laufs, der die Config erzeugt hat. Nach dem Backtester-Lookahead-Fix galten so
43/44 Configs weiter als "bestaetigt", obwohl sie die Pruefung mit dem
korrigierten Backtester nicht mehr bestehen -- und run_portfolio_optimizer.py
waehlt nur unter bestaetigten Configs aus.

Gleiches Fenster und gleiche Kriterien wie optimizer.py / auto_parameter_optimizer_scheduler.py:
letzte backtest_lookback_weeks bis gestern, chronologischer IS/OOS-Split per
is_fraction, Kriterien per optimizer.oos_gate() (min_oos_trades,
min_oos_profit_factor, max_drawdown_pct aus settings.json).

Aufruf:
  python sync_confirmed_flags.py            # alle Configs neu bewerten + schreiben
  python sync_confirmed_flags.py --dry-run  # nur anzeigen
"""
import argparse
import glob
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.analysis.backtester import load_data, run_envelope_backtest, FINE_TF_MAP  # noqa: E402
from ltbbot.analysis.optimizer import oos_gate  # noqa: E402

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')


def main():
    parser = argparse.ArgumentParser(description='_meta.confirmed aller Configs neu bewerten')
    parser.add_argument('--dry-run', action='store_true', help='Nur anzeigen, nichts schreiben')
    parser.add_argument('--end-date', type=str, default=None, help='Fensterende (Standard: gestern)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
        opt = json.load(f).get('optimization_settings', {})
    lookback_weeks = int(opt.get('backtest_lookback_weeks', 26))
    is_fraction = float(opt.get('is_fraction', 0.7))
    min_trades = int(opt.get('min_oos_trades', 10))
    min_pf = float(opt.get('min_oos_profit_factor', 1.3))
    max_dd = float(opt.get('constraints', {}).get('max_drawdown_pct', 30)) / 100.0
    start_capital = float(opt.get('start_capital', 10))

    end_date = args.end_date or (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    start_date = (date.fromisoformat(end_date) - timedelta(weeks=lookback_weeks)).strftime('%Y-%m-%d')
    print(f"Fenster {start_date} -> {end_date} | IS-Anteil {is_fraction} | "
          f"Kriterien: OOS-Trades>={min_trades}, PnL>0, PF>={min_pf}, MaxDD<={max_dd*100:.0f}%\n")

    rows = []
    for path in sorted(glob.glob(os.path.join(CONFIGS_DIR, 'config_*_envelope.json'))):
        fname = os.path.basename(path)
        try:
            with open(path) as f:
                cfg = json.load(f)
            symbol, timeframe = cfg['market']['symbol'], cfg['market']['timeframe']
            data = load_data(symbol, timeframe, start_date, end_date)
            if data is None or data.empty:
                print(f"  {fname}: keine Daten -- unveraendert")
                continue
            split_idx = int(len(data) * is_fraction)
            split_ts = data.index[split_idx]
            fine_tf = FINE_TF_MAP.get(timeframe)
            fine = load_data(symbol, fine_tf, start_date, end_date) if fine_tf else None
            if fine is not None and fine.empty:
                fine = None
            params = {'strategy': cfg['strategy'], 'risk': cfg['risk'],
                      'behavior': cfg.get('behavior', {'use_longs': True, 'use_shorts': True})}
            res_is = run_envelope_backtest(data.iloc[:split_idx].copy(), params, start_capital,
                                           show_progress=False, fine_data=fine, multi_band_entries=True)
            res_oos = run_envelope_backtest(data.iloc[split_idx:].copy(), params, start_capital,
                                            show_progress=False, fine_data=fine, multi_band_entries=True)
            gate = oos_gate(res_oos, min_trades, min_pf, max_dd)
            was = cfg.get('_meta', {}).get('confirmed')
            rows.append((fname, was, gate['passed'], res_oos.get('trades_count', 0),
                         res_oos.get('total_pnl_pct', 0), gate['profit_factor_display']))
            if not args.dry_run:
                cfg.setdefault('_meta', {}).update({
                    'pnl_pct': round(res_is.get('total_pnl_pct', 0), 2),
                    'oos_pnl_pct': round(res_oos.get('total_pnl_pct', 0), 2),
                    'oos_trades': res_oos.get('trades_count', 0),
                    'oos_profit_factor': round(gate['profit_factor_display'], 2),
                    'oos_win_rate': round(gate['win_rate'], 2),
                    'oos_max_drawdown_pct': round(gate['max_dd_decimal'] * 100, 2),
                    'is_oos_split_date': str(split_ts.date()),
                    'is_fraction': is_fraction,
                    'confirmed': gate['passed'],
                    'rechecked_at': datetime.now().isoformat(timespec='seconds'),
                })
                with open(path, 'w') as f:
                    json.dump(cfg, f, indent=4)
        except Exception as e:
            print(f"  {fname}: Fehler ({e}) -- unveraendert")

    print(f"{'Config':<42}{'vorher':>8}{'jetzt':>8}{'OOS-Tr':>8}{'OOS-PnL':>10}{'PF':>7}")
    for fname, was, now, n, pnl, pf in rows:
        print(f"{fname:<42}{str(was):>8}{str(now):>8}{n:>8}{pnl:>+9.1f}%{pf:>7.2f}")
    n_conf = sum(1 for r in rows if r[2])
    print(f"\n{n_conf}/{len(rows)} Configs bestaetigt" + (" (dry-run, nichts geschrieben)" if args.dry_run else ""))


if __name__ == '__main__':
    main()
