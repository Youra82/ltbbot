#!/usr/bin/env python3
"""Groesse und Hebel aller RT-Configs aus settings.json::rt_settings setzen (2026-11-07, F2).

Warum ein eigenes Skript: update.sh stellt auf dem VPS die VPS-eigenen Configs (taegliche
Parameter-Suche) wieder her -- im Repo geaenderte Risiko-Werte kaemen dort nie an. Dieses
Skript laeuft am Ende von update.sh und ist wiederholbar: es aendert NUR
risk.position_size_pct / short_position_size_pct / leverage / short_leverage, nie die
Signal-Parameter (Mitte, Baender, SL).

F2 (User-Wahl 2026-11-07): position_size_pct 15, Shorts 7.5, Hebel je Config = int(0.8 / SL)
zwischen rt_settings.leverage und max_leverage, Shorts eigener Hebel aus dem Short-SL.

Aufruf: python3 apply_rt_sizing.py [--dry-run]
"""
import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'src'))
from ltbbot.strategy.envelope_logic import leverage_for_sl  # noqa: E402

CONFIGS = os.path.join(ROOT, 'src', 'ltbbot', 'strategy', 'configs', 'config_*_envelope.json')


def target_risk(risk, rt):
    """Risiko-Block nach rt_settings (nur Groesse/Hebel), Rest unveraendert."""
    out = dict(risk)
    out['position_size_pct'] = float(rt['position_size_pct'])
    short = rt.get('short') or {}
    if risk.get('short_position_size_pct') is not None and short.get('position_size_pct') is not None:
        out['short_position_size_pct'] = float(short['position_size_pct'])
    if rt.get('leverage_mode') == 'per_sl':
        out['leverage'] = leverage_for_sl(risk.get('stop_loss_pct'), rt.get('max_leverage', 8), rt.get('leverage', 2))
        if risk.get('short_stop_loss_pct'):
            out['short_leverage'] = leverage_for_sl(risk['short_stop_loss_pct'], rt.get('max_leverage', 8), 1)
    else:
        out['leverage'] = int(rt.get('leverage', 1))
        out.pop('short_leverage', None)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    with open(os.path.join(ROOT, 'settings.json'), encoding='utf-8') as f:
        s = json.load(f)
    if (s.get('optimization_settings') or {}).get('strategy_mode') != 'rt' or 'rt_settings' not in s:
        print('apply_rt_sizing: kein RT-Modus -- nichts zu tun.')
        return 0
    rt = s['rt_settings']
    changed = skipped = 0
    for path in sorted(glob.glob(CONFIGS)):
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
        risk = cfg.get('risk') or {}
        if risk.get('sizing') != 'fraction':
            skipped += 1
            continue
        new = target_risk(risk, rt)
        if new != risk:
            changed += 1
            if not args.dry_run:
                cfg['risk'] = new
                tmp = path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, indent=4, ensure_ascii=False)
                    f.write('\n')
                os.replace(tmp, path)
    print(f"apply_rt_sizing: {changed} Configs angepasst{' (dry-run)' if args.dry_run else ''}, "
          f"{skipped} ohne Kapitalanteil-Groesse uebersprungen.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
