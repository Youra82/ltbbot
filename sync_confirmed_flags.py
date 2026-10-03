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
import time
from datetime import date, datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.analysis.backtester import load_data, run_envelope_backtest, FINE_TF_MAP  # noqa: E402
from ltbbot.analysis.optimizer import oos_gate  # noqa: E402
from ltbbot.strategy.envelope_logic import median_atr_pct, sl_atr_fraction, band_structure_ok  # noqa: E402

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')


def main():
    parser = argparse.ArgumentParser(description='_meta.confirmed aller Configs neu bewerten')
    parser.add_argument('--dry-run', action='store_true', help='Nur anzeigen, nichts schreiben')
    parser.add_argument('--only', type=str, default=None,
                        help='Nur diese Coins pruefen, kommagetrennt (z.B. BGB,FIL) -- fuer Wiederholungen nach Download-Fehlern')
    parser.add_argument('--end-date', type=str, default=None, help='Fensterende (Standard: gestern)')
    parser.add_argument('--sl-check-only', action='store_true',
                        help='Nur die Stop-/Band-Abstands-Regeln pruefen (schnell, ohne Backtests): Configs mit zu '
                             'engem Stop oder zu engen Baendern werden auf confirmed=false gesetzt, '
                             'alle anderen bleiben unveraendert.')
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
    min_sl_atr = float(opt.get('min_sl_atr_fraction', 0.06))
    min_env1_atr = float(opt.get('min_env1_atr_fraction', 0.5))
    min_gap_atr = float(opt.get('min_band_gap_atr_fraction', 0.25))

    end_date = args.end_date or (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    start_date = (date.fromisoformat(end_date) - timedelta(weeks=lookback_weeks)).strftime('%Y-%m-%d')
    print(f"Fenster {start_date} -> {end_date} | IS-Anteil {is_fraction} | "
          f"Kriterien: OOS-Trades>={min_trades}, PnL>0, PF>={min_pf}, MaxDD<={max_dd*100:.0f}%, "
          f"Stop >= {min_sl_atr*100:.0f}%, Band 1 >= {min_env1_atr*100:.0f}%, Band-Luecken >= {min_gap_atr*100:.0f}% der typischen Kerze\n")

    rows = []
    for path in sorted(glob.glob(os.path.join(CONFIGS_DIR, 'config_*_envelope.json'))):
        fname = os.path.basename(path)
        if args.only and not any(fname.startswith(f"config_{c.strip().upper()}USDT") for c in args.only.split(',')):
            continue
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
            params = {'strategy': cfg['strategy'], 'risk': cfg['risk'],
                      'behavior': cfg.get('behavior', {'use_longs': True, 'use_shorts': True})}
            is_atr = median_atr_pct(data.iloc[:split_idx])
            sl_frac = sl_atr_fraction(params, is_atr)
            sl_ok = (sl_frac is None or sl_frac >= min_sl_atr) and band_structure_ok(params, is_atr, min_env1_atr, min_gap_atr)
            was = cfg.get('_meta', {}).get('confirmed')
            if args.sl_check_only:
                now = bool(was) and sl_ok
                rows.append((fname, was, now, None, None, None, sl_frac))
                if not args.dry_run and now != bool(was):
                    cfg.setdefault('_meta', {}).update({
                        'confirmed': now, 'sl_atr_fraction': round(sl_frac, 4),
                        'rechecked_at': datetime.now().isoformat(timespec='seconds'),
                        'unconfirmed_reason': 'stop_or_bands_too_tight_vs_atr'})
                    with open(path, 'w') as f:
                        json.dump(cfg, f, indent=4)
                continue
            fine_tf = FINE_TF_MAP.get(timeframe)
            fine = None
            for _attempt in range(3):
                fine = load_data(symbol, fine_tf, start_date, end_date) if fine_tf else None
                if fine is not None and fine.empty:
                    fine = None
                if fine is not None or not fine_tf:
                    break
                time.sleep(10)
            if fine_tf and fine is None:
                # Ohne Fein-Daten wuerde die alte, zu optimistische Entry-Kerzen-Regel greifen
                # (siehe simulate_entry_fill) -> lieber NICHT bestaetigen und neu pruefen lassen.
                print(f"  {fname}: keine {fine_tf}-Feindaten (Download-Fehler) -- als NICHT bestaetigt markiert, "
                      f"spaeter mit --only {symbol.split('/')[0]} wiederholen")
                rows.append((fname, was, False, None, None, None, sl_frac))
                if not args.dry_run:
                    cfg.setdefault('_meta', {}).update({
                        'confirmed': False, 'unconfirmed_reason': 'no_fine_data_recheck',
                        'rechecked_at': datetime.now().isoformat(timespec='seconds')})
                    with open(path, 'w') as f:
                        json.dump(cfg, f, indent=4)
                continue
            res_is = run_envelope_backtest(data.iloc[:split_idx].copy(), params, start_capital,
                                           show_progress=False, fine_data=fine, multi_band_entries=True)
            res_oos = run_envelope_backtest(data.iloc[split_idx:].copy(), params, start_capital,
                                            show_progress=False, fine_data=fine, multi_band_entries=True)
            gate = oos_gate(res_oos, min_trades, min_pf, max_dd)
            if not sl_ok:
                gate['passed'] = False
            rows.append((fname, was, gate['passed'], res_oos.get('trades_count', 0),
                         res_oos.get('total_pnl_pct', 0), gate['profit_factor_display'], sl_frac))
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
                    'sl_atr_fraction': round(sl_frac, 4) if sl_frac is not None else None,
                    'rechecked_at': datetime.now().isoformat(timespec='seconds'),
                })
                if gate['passed']:
                    cfg['_meta'].pop('unconfirmed_reason', None)
                with open(path, 'w') as f:
                    json.dump(cfg, f, indent=4)
        except Exception as e:
            print(f"  {fname}: Fehler ({e}) -- unveraendert")

    print(f"{'Config':<42}{'vorher':>8}{'jetzt':>8}{'OOS-Tr':>8}{'OOS-PnL':>10}{'PF':>7}{'Stop/ATR':>10}")
    for fname, was, now, n, pnl, pf, frac in rows:
        frac_s = f"{frac*100:.0f}%" if frac is not None else '-'
        n_s = f"{n:>8}" if n is not None else f"{'-':>8}"
        pnl_s = f"{pnl:>+9.1f}%" if pnl is not None else f"{'-':>10}"
        pf_s = f"{pf:>7.2f}" if pf is not None else f"{'-':>7}"
        print(f"{fname:<42}{str(was):>8}{str(now):>8}{n_s}{pnl_s}{pf_s}{frac_s:>10}")
    n_conf = sum(1 for r in rows if r[2])
    print(f"\n{n_conf}/{len(rows)} Configs bestaetigt" + (" (dry-run, nichts geschrieben)" if args.dry_run else ""))


if __name__ == '__main__':
    main()
