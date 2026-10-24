#!/usr/bin/env python3
"""
daily_live_vs_backtest_check.py  (ltbbot)

Vergleicht einmal taeglich die echten Bitget-Trades der letzten N Tage Trade fuer Trade
mit dem Portfolio-Simulator (Envelope-Baender, Margin-Topf und Positionsgrenze wie live)
und sendet das Ergebnis per Telegram. Ersetzt den alten Summen-je-Coin-Check (2026-10-23),
uebernommen von stbot und angepasst an ltbbot: der Simulator fuehrt jedes Band als eigenen
Trade, Bitget eine Position je Symbol -- Baender werden dafuer zu Positionen zusammengefasst.

Anders als ltbbot zaehlt nicht nur Summen je Coin:
  - jede Live-Position wird ihrer Simulator-Position zugeordnet (gleiches Symbol, gleiche
    Richtung, erstes Band live in derselben Kerze gefuellt)
  - fehlende / zusaetzliche Trades, abweichende Exits (> EXIT_TOL_PCT) einzeln
  - verwaiste und doppelte Schutz-Orders (SL/TP ohne Position bzw. mehrfach; Einstiegs-Trigger
    der Baender zaehlen nicht)
Welche Strategien zum Zeitpunkt eines Signals live aktiv waren, kommt aus der
Teamhistorie (artifacts/db/team_history.jsonl, geschrieben von master_runner.py).

Wird von master_runner.py bei jedem 15-Min-Zyklus im Hintergrund gestartet und prueft
selbst, ob ein Lauf faellig ist (einmal pro Tag zur konfigurierten Stunde, UTC).

Aufruf:
  python3 daily_live_vs_backtest_check.py                  # normale Pruefung (faellig?)
  python3 daily_live_vs_backtest_check.py --force          # sofort, nur Ausgabe
  python3 daily_live_vs_backtest_check.py --force --send   # sofort, mit Telegram
  --history PATH   andere Teamhistorie (Tests/Nachtrag), alternativ Umgebungsvariable LTBBOT_TEAM_HISTORY
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

import ccxt
import pandas as pd

from ltbbot.analysis.backtester import load_data, FINE_TF_MAP, LazyFineData
from ltbbot.analysis.portfolio_simulator import run_portfolio_simulation
from ltbbot.utils import team_history
from ltbbot.utils.telegram import send_document, send_message, send_photo
from ltbbot.analysis.live_vs_backtest_report import write_report

LOG_DIR       = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE = os.path.join(PROJECT_ROOT, 'settings.json')
SECRET_FILE   = os.path.join(PROJECT_ROOT, 'secret.json')
CONFIGS_DIR   = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
LAST_RUN_FILE = os.path.join(PROJECT_ROOT, 'data', 'cache', '.last_live_vs_backtest_check')  # wie der alte Check
TRIGGER_LOG   = os.path.join(LOG_DIR, 'daily_live_vs_backtest_check.log')
REPORT_DIR    = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'reports')  # gitignored
PRODUCT       = 'USDT-FUTURES'

DEFAULT_SETTINGS = {'enabled': True, 'rolling_window_days': 7, 'send_hour': 8}
MATCH_MAX_DELAY = pd.Timedelta(minutes=20)  # Kulanz nach Ende der Fuellkerze (15-Min-Takt)
EXIT_TOL_PCT = 0.5
TF_MIN = {'15m': 15, '30m': 30, '1h': 60, '2h': 120, '4h': 240, '6h': 360, '1d': 1440}


def _log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(TRIGGER_LOG, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} LIVE-VS-BACKTEST {msg}\n")


def _settings():
    with open(SETTINGS_FILE, encoding='utf-8') as f:
        s = json.load(f)
    return {**DEFAULT_SETTINGS, **s.get('live_vs_backtest_check_settings', {})}


def _is_due(send_hour):
    now = datetime.now(timezone.utc)
    if now.hour < send_hour:
        return False
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE) as f:
            if f.read().strip() == now.strftime('%Y-%m-%d'):
                return False
    return True


def _set_last_run():
    os.makedirs(os.path.dirname(LAST_RUN_FILE), exist_ok=True)
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(datetime.now(timezone.utc).strftime('%Y-%m-%d'))


def _secrets():
    """Schluessel aus Umgebungsvariablen (Tests) oder secret.json."""
    if os.environ.get('LTBBOT_API_KEY'):
        acc = {'apiKey': os.environ['LTBBOT_API_KEY'], 'secret': os.environ.get('LTBBOT_API_SECRET'),
               'password': os.environ.get('LTBBOT_API_PASSWORD')}
        tg = {'bot_token': os.environ.get('LTBBOT_TG_TOKEN'), 'chat_id': os.environ.get('LTBBOT_TG_CHAT')}
        return acc, tg
    with open(SECRET_FILE) as f:
        s = json.load(f)
    return s['ltbbot'][0], s.get('telegram', {})


# --- Live-Seite ---------------------------------------------------------------

def _paginate(call, base, list_key):
    rows, cursor = [], None
    for _ in range(100):
        params = dict(base, **({'idLessThan': cursor} if cursor else {}))
        data = call(params).get('data') or {}
        batch = data.get(list_key) or []
        rows.extend(batch)
        end_id = data.get('endId')
        if not batch or not end_id or end_id == cursor or len(batch) < 100:
            break
        cursor = end_id
        time.sleep(0.2)
    return rows


def fetch_live(acc, since):
    ex = ccxt.bitget({'apiKey': acc.get('apiKey'), 'secret': acc.get('secret'), 'password': acc.get('password'),
                      'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    lookback = since - pd.Timedelta(days=60)  # Positionen, die vor dem Fenster eroeffnet wurden
    closed = _paginate(ex.privateMixGetV2MixPositionHistoryPosition,
                       {'productType': PRODUCT, 'startTime': str(int(lookback.timestamp() * 1000)), 'limit': '100'},
                       'list')
    open_pos = ex.privateMixGetV2MixPositionAllPosition({'productType': PRODUCT, 'marginCoin': 'USDT'})['data'] or []
    plans = []
    for pt in ('profit_loss', 'normal_plan', 'track_plan'):
        d = ex.privateMixGetV2MixOrderOrdersPlanPending({'productType': PRODUCT, 'planType': pt})['data'] or {}
        # Einstiegs-Trigger der Baender (tradeSide open) sind normal und keine Schutz-Orders
        plans += [dict(o, planType=pt) for o in (d.get('entrustedList') or []) if o.get('tradeSide') != 'open']
    acct = ex.privateMixGetV2MixAccountAccounts({'productType': PRODUCT})['data']
    equity = next((float(a['accountEquity']) for a in acct if a['marginCoin'] == 'USDT'), None)

    ms = lambda x: pd.Timestamp(int(x), unit='ms', tz='UTC')
    trades = [dict(symbol=p['symbol'], side=p['holdSide'], entry_time=ms(p['ctime']), exit_time=ms(p['utime']),
                   entry=float(p['openAvgPrice']), exit=float(p['closeAvgPrice']), pnl=float(p['netProfit']))
              for p in closed if ms(p['ctime']) >= since]
    trades += [dict(symbol=p['symbol'], side=p['holdSide'], entry_time=ms(p['cTime']), exit_time=None,
                    entry=float(p['openPriceAvg']), exit=None, pnl=float(p['unrealizedPL']))
               for p in open_pos if ms(p['cTime']) >= since]
    # Vor dem Fenster eroeffnet, im Fenster noch offen -> blockiert das Symbol fuer neue Einstiege
    carry = [dict(symbol=p['symbol'], entry_time=ms(p['ctime']), exit_time=ms(p['utime']))
             for p in closed if ms(p['ctime']) < since <= ms(p['utime'])]
    carry += [dict(symbol=p['symbol'], entry_time=ms(p['cTime']), exit_time=None)
              for p in open_pos if ms(p['cTime']) < since]
    return sorted(trades, key=lambda t: t['entry_time']), carry, open_pos, plans, equity


def check_orders(open_pos, plans):
    """Verwaiste Schutz-Orders (kein offenes Symbol) und doppelte (gleicher Typ+Trigger je Symbol)."""
    flags = []
    open_syms = {p['symbol'] for p in open_pos}
    for o in plans:
        if o['symbol'] not in open_syms:
            kind = {'track_plan': 'TP (Trailing)', 'profit_loss': 'TP/SL'}.get(o['planType'], 'SL/TP')
            since = pd.Timestamp(int(o['cTime']), unit='ms').strftime('%d.%m.')
            flags.append(f"Verwaiste {kind}-Order {o['symbol'][:-4]} (seit {since}, keine Position)")
    seen = {}
    for o in plans:
        k = (o['symbol'], o['planType'], o['triggerPrice'])
        seen[k] = seen.get(k, 0) + 1
    for (sym, pt, trig), n in seen.items():
        if n > 1:
            kind = {'track_plan': 'TP (Trailing)', 'profit_loss': 'TP/SL'}.get(pt, 'SL/TP')
            flags.append(f"{n}x gleiche {kind}-Order {sym[:-4]} @ {trig}")
    return flags


def positions_without_sl(open_pos, plans):
    """Offene Positionen ohne Stop-Loss bei Bitget. SL = Schliess-Order auf der Verlustseite des
    aktuellen Kurses (Long: Trigger unter dem Kurs, Short: darueber) oder ein an der Einstiegs-
    Order haengender SL (stopLossTriggerPrice). TP wird bewusst nicht verlangt -- mehrere Bots
    steigen ohne TP-Order aus (Trailing, Gegen-Brick, Market-Close)."""
    flags = []
    for p in open_pos:
        side, sym = p.get('holdSide'), p.get('symbol')
        try:
            mark = float(p.get('markPrice') or p.get('openPriceAvg') or 0)
        except (TypeError, ValueError):
            mark = 0.0
        has_sl = False
        for o in plans:
            if o.get('symbol') != sym or o.get('tradeSide') == 'open':
                continue
            if o.get('posSide') and side and o['posSide'] != side:
                continue
            if str(o.get('stopLossTriggerPrice') or '0') not in ('', '0', '0.0'):
                has_sl = True
                break
            try:
                trig = float(o.get('triggerPrice') or 0)
            except (TypeError, ValueError):
                continue
            if o.get('planType') != 'track_plan' and trig > 0 and mark > 0 and (
                    (side == 'long' and trig < mark) or (side == 'short' and trig > mark)):
                has_sl = True
                break
        if not has_sl:
            flags.append(f"❗ Position {sym[:-4]} {side} OHNE Stop-Loss bei Bitget (Kurs {mark:g})")
    return flags


# --- Simulator-Seite ----------------------------------------------------------

def _symbol_raw(key):
    return key.rsplit('_', 1)[0][:-4]  # ENAUSDTUSDT_15m -> ENAUSDT


def active_periods(history, key):
    """Zeitraeume (von, bis), in denen die Strategie laut Teamhistorie im Live-Team war."""
    periods, since = [], None
    for e in history:
        ts = pd.Timestamp(e['valid_from'])
        if key in e['team'] and since is None:
            since = ts
        elif key not in e['team'] and since is not None:
            periods.append((since, ts))
            since = None
    if since is not None:
        periods.append((since, None))
    return periods


def _warmup_start(start, timeframe):
    """Vorlauf fuer Indikatoren/Baender wie im alten ltbbot-Check (300 Kerzen, mind. 14 Tage)."""
    hours = TF_MIN.get(timeframe, 1440) / 60
    return (start - timedelta(days=max(int(300 * hours / 24) + 1, 14))).strftime('%Y-%m-%d')


def group_positions(layers, key, end):
    """Simulator-Baender einer Strategie zu Positionen wie bei Bitget zusammenfassen: eine Position
    laeuft vom ersten gefuellten Band, bis kein Band derselben Seite mehr offen ist."""
    layers = sorted(layers, key=lambda l: l['entry_time'])
    out, cur = [], None
    for l in layers:
        if cur and l['side'] == cur['side'] and (cur['_open'] or l['entry_time'] <= cur['_last_exit']):
            cur['_layers'].append(l)
            cur['_open'] = cur['_open'] or l['exit_time'] is None
            if l['exit_time'] is not None:
                cur['_last_exit'] = max(cur['_last_exit'], l['exit_time'])
            continue
        cur = {'side': l['side'], '_layers': [l], '_open': l['exit_time'] is None,
               '_last_exit': l['exit_time'] if l['exit_time'] is not None else l['entry_time']}
        out.append(cur)
    positions = []
    for c in out:
        ls = c['_layers']
        amt = sum(l['amount'] for l in ls) or 1.0
        closed = [l for l in ls if l['exit_time'] is not None]
        camt = sum(l['amount'] for l in closed) or 1.0
        positions.append(dict(
            key=key, symbol=_symbol_raw(key), side=c['side'], signal_time=ls[0]['entry_time'],
            exit_time=None if c['_open'] else c['_last_exit'],
            entry=sum(l['entry'] * l['amount'] for l in ls) / amt,
            exit=None if c['_open'] else sum(l['exit'] * l['amount'] for l in closed) / camt,
            pnl=None if c['_open'] else sum(l['pnl'] for l in ls), bands=len(ls)))
    return positions


def simulate(history, start, end, capital, carry):
    """Ein Portfolio-Lauf wie der Livebot: Envelope-Baender, gemeinsamer Margin-Topf,
    Positionsgrenze, Einstiege nur waehrend der Team-Zugehoerigkeit, danach nur noch TP/SL,
    Symbole mit aelterer Live-Position gesperrt. Ergebnis je Position (Baender zusammengefasst)."""
    end_s = (end + timedelta(days=1)).strftime('%Y-%m-%d')
    keys = sorted({k for e in history for k in e['team'] if pd.Timestamp(e['valid_from']) <= end})
    sd, data_flags = {}, []
    for key in keys:
        periods = [(a, b) for a, b in active_periods(history, key) if b is None or b > start]
        if not periods:
            continue
        path = os.path.join(CONFIGS_DIR, f'config_{key}{team_history.CONFIG_SUFFIX}.json')
        if not os.path.exists(path):
            data_flags.append(f"Config {_short(key)} fehlt – Strategie nicht simuliert")
            continue
        with open(path) as f:
            cfg = json.load(f)
        sym, tf = cfg['market']['symbol'], cfg['market']['timeframe']
        data = load_data(sym, tf, _warmup_start(start, tf), end_s)
        if data is not None and not data.empty:
            data = data[~data.index.duplicated()].sort_index()
            data = data[data.index + pd.Timedelta(minutes=TF_MIN[tf]) <= end]  # nur geschlossene Kerzen
        # Ohne vollstaendige Kerzen waere jede "Abweichung" nur ein Datenloch des Backtests
        last = data.index.max() if data is not None and len(data) else None
        if last is None or last < end - 2 * pd.Timedelta(minutes=TF_MIN[tf]):
            data_flags.append(f"Backtest-Daten {_short(key)} ({tf}) unvollständig: enden "
                              f"{_fmt_t(last) if last is not None else 'leer'} – Ergebnis dieser Strategie ungültig")
            if last is None:
                continue
        fine_tf = FINE_TF_MAP.get(tf)
        sd[key] = {'symbol': sym, 'timeframe': tf, 'data': data, 'params': cfg,
                   'fine_data': LazyFineData(sym, fine_tf) if fine_tf else None, 'active_periods': periods}
    if not sd:
        return [], data_flags
    blocks = [(f"{c['symbol'][:-4]}/USDT:USDT", c['entry_time'], c['exit_time']) for c in carry]
    res = run_portfolio_simulation(capital, sd, start.strftime('%Y-%m-%d'), end_s, multi_band_entries=True,
                                   symbol_blocks=blocks) or {}

    def as_utc(x):
        x = pd.Timestamp(x)
        return x.tz_localize('UTC') if x.tzinfo is None else x

    layers = {}
    tdf = res.get('trades_df')
    if tdf is not None and not tdf.empty:
        for t in tdf.to_dict('records'):
            layers.setdefault(t['strategy_id'], []).append(dict(
                side=t['side'], entry_time=as_utc(t['entry_time']), exit_time=as_utc(t['exit_time']),
                entry=t['entry_price'], exit=t['exit_price'], amount=t['amount_coins'], pnl=t['pnl_usd']))
    for l in res.get('open_positions', []):
        layers.setdefault(l['strategy_id'], []).append(dict(
            side=l['side'], entry_time=as_utc(l.get('entry_time')), exit_time=None,
            entry=l['entry_price'], exit=None, amount=l.get('amount_coins', 0.0), pnl=None))
    trades = [p for key, ls in layers.items() for p in group_positions(ls, key, end)]
    return [t for t in trades if t['signal_time'] >= start], data_flags


# --- Ursachen aus den Bot-Logs ------------------------------------------------

# (Suchtext im Log, Ursache) -- Reihenfolge = Prioritaet, Texte aus trade_manager.py
LOG_REASONS = [
    ('Positionen offen (Grenze', 'Positionsgrenze erreicht, keine neuen Einstiege'),
    ('wuerde freies', 'zu wenig freie Margin'),
    ('Freies Guthaben', 'zu wenig freies Guthaben'),
    ('Nicht genügend Guthaben', 'zu wenig Guthaben'),
    ('unter Bitget-Minimum', 'Order unter Bitget-Minimum'),
    ('liegt unter Minimum', 'Menge unter Bitget-Minimum'),
    ('Nicht genügend Daten', 'Kerzendaten unvollständig'),
    ('Börsenfehler', 'Börsenfehler'),
    ('nicht mehr in active_strategies', 'Strategie live nicht im Team'),
    ('STRONG_TREND', 'starker Trend erkannt, keine Einstiege'),
    ('STARKER TREND', 'starker Trend erkannt, keine Einstiege'),
    ('gesperrt (BTC-Trend', 'Einstiege gesperrt (BTC-Trend oder Sperre nach SL)'),
    ('Einstiege gesperrt', 'Einstiege gesperrt (Sperre nach SL)'),
    ('DEAKTIVIERT', 'Richtung durch Trendfilter gesperrt'),
    ('sofortiger SL vermieden', 'Band übersprungen, Kurs schon jenseits SL'),
    ('Kerze begann schon jenseits SL', 'Band übersprungen, Kurs schon jenseits SL'),
    ('Kein Close-Confirmation', 'keine Close-Bestätigung, Band übersprungen'),
    ('platziert:', 'Einstiegs-Orders lagen, live nicht gefüllt'),
    ('Keine Entry-Orders platziert', 'keine Einstiegs-Orders platziert'),
]


def _log_lines(key):
    """Zeilen aus logs/ltbbot_<KEY>.log inkl. rotierter Dateien (.1-.3), aelteste zuerst."""
    base = os.path.join(LOG_DIR, f'ltbbot_{key}.log')
    lines = []
    for path in [f'{base}.{i}' for i in (3, 2, 1)] + [base]:
        if os.path.exists(path):
            with open(path, encoding='utf-8', errors='replace') as f:
                lines += f.readlines()
    return lines if os.path.exists(base) else None


def explain_from_log(key, decision_time, side=None):
    """Ursache aus dem Bot-Log waehrend der Fuellkerze (Log-Zeit = Ortszeit des Rechners)."""
    lines = _log_lines(key)
    if lines is None:
        return 'kein Bot-Log vorhanden'
    local_tz = datetime.now().astimezone().tzinfo
    t0, t1 = decision_time - pd.Timedelta(minutes=1), decision_time + _tf_dur(key)
    window = []
    for line in lines:
        try:
            ts = pd.Timestamp(line[:19]).tz_localize(local_tz).tz_convert('UTC')
        except (ValueError, TypeError):
            continue
        if t0 <= ts <= t1:
            text = line[26:].strip()
            low = text.lower()
            # Zeilen nur ueber die andere Richtung (z. B. "short-Einstiege gesperrt" bei einem
            # Long) erklaeren nichts
            if side and {'long': 'short', 'short': 'long'}.get(side, '') in low and side not in low:
                continue
            window.append(text)
    if not window:
        return 'Bot lief in dem Zeitraum nicht (kein Log-Eintrag)'
    for needle, reason in LOG_REASONS:
        hit = next((l for l in window if needle in l), None)
        if hit:
            return f"{reason} | Log: {hit[:120]}"
    return f"unklar | Log: {window[-1][:120]}"


def explain_exit(st, lt):
    """Exit-Abweichung: frueherer/spaeterer Exit; der Simulator prueft TP/SL je Kerze und Band."""
    tf = st['key'].rsplit('_', 1)[1]
    when = 'früher' if lt['exit_time'] < st['exit_time'] else 'später'
    return (f"live {when} raus ({_fmt_t(lt['exit_time'])}, Simulator in Kerze ab {_fmt_t(st['exit_time'])}); "
            f"Simulator: {st.get('bands', 1)} Band/Bänder, TP/SL auf {tf}-Kerzen, "
            f"Gleichstand per {FINE_TF_MAP.get(tf)}-Kerzen")


# --- Abgleich -----------------------------------------------------------------

def compare(live, sim, history_path):
    team_at = lambda ts: team_history.team_at(ts, path=history_path)
    used, rows, extra, unknown = set(), [], [], []
    for lt in live:
        team = team_at(lt['entry_time'])
        if team is None:
            unknown.append(lt)
            continue
        cand = [i for i, st in enumerate(sim) if i not in used and st['symbol'] == lt['symbol']
                and st['side'] == lt['side']
                and timedelta(0) <= lt['entry_time'] - st['signal_time'] <= _tf_dur(st['key']) + MATCH_MAX_DELAY]
        if cand:
            used.add(cand[0])
            rows.append((lt, sim[cand[0]]))
        else:
            in_team = [k for k in team if _symbol_raw(k) == lt['symbol']]
            extra.append((lt, in_team))
    missing = [st for i, st in enumerate(sim) if i not in used]
    return rows, extra, missing, unknown


def _fmt_t(ts):
    return ts.strftime('%d.%m. %H:%M')


def _short(key):
    sym, tf = key.rsplit('_', 1)
    return f"{sym[:-8]}/{tf}"  # ENAUSDTUSDT_15m -> ENA/15m


def portfolio_lines(history, start, end):
    """Welches Portfolio galt im Fenster, was hat sich wann geaendert, was gilt jetzt."""
    relevant = [e for e in history if pd.Timestamp(e['valid_from']) <= end]
    if not relevant:
        return ["Portfolio: keine Teamhistorie vorhanden"]
    at_start = [e for e in relevant if pd.Timestamp(e['valid_from']) <= start]
    lines = ["Portfolio im Zeitraum:"]
    prev = at_start[-1] if at_start else None
    if prev:
        lines.append(f"• ab {_fmt_t(pd.Timestamp(prev['valid_from']))}: "
                     + ", ".join(_short(k) for k in prev['team']))
    for e in relevant:
        ts = pd.Timestamp(e['valid_from'])
        if ts <= start:
            continue
        if prev is None:
            lines.append(f"• ab {_fmt_t(ts)} ({e['source']}): " + ", ".join(_short(k) for k in e['team']))
        else:
            added = [_short(k) for k in e['team'] if k not in prev['team']]
            removed = [_short(k) for k in prev['team'] if k not in e['team']]
            changed = [_short(k) for k in e['team'] if k in prev['team'] and None not in (e['team'][k], prev['team'][k])
                       and e['team'][k] != prev['team'][k]]
            parts = ([f"+{', +'.join(added)}"] if added else []) + ([f"−{', −'.join(removed)}"] if removed else []) \
                + ([f"Config neu: {', '.join(changed)}"] if changed else [])
            lines.append(f"• {_fmt_t(ts)} {e['source']}: " + (" | ".join(parts) if parts else "unverändert"))
        prev = e
    lines.append(f"Jetzt gültig ({len(prev['team'])}): " + ", ".join(_short(k) for k in prev['team']))
    return lines


def _tf_dur(key):
    return pd.Timedelta(minutes=TF_MIN[key.rsplit('_', 1)[1]])


def build_items(rows, extra, missing, live, end):
    """Eine Zeile je Fall (gleich / fehlt live / nur live) mit Ursache -- Grundlage fuer
    Telegram-Text und illustrierte Uebersicht, damit beide dasselbe sagen."""
    def lt_d(lt):
        return dict(entry_time=lt['entry_time'], exit_time=lt['exit_time'], entry=lt['entry'],
                    exit=lt['exit'], pnl=lt['pnl'] if lt['exit_time'] is not None else None)

    def st_d(st):
        return dict(signal_time=st['signal_time'], exit_time=st['exit_time'], entry=st['entry'],
                    exit=st['exit'], pnl=st['pnl'])

    items = []
    for lt, st in rows:
        d = dict(status='gleich', key=st['key'], name=_short(st['key']), side=st['side'],
                 live=lt_d(lt), sim=st_d(st), exit_dev_pct=None, exit_note='', reason='')
        if lt['exit'] is None or st['exit'] is None:
            pending = st['exit'] is None and lt['exit_time'] is not None and lt['exit_time'] > end - _tf_dur(st['key'])
            if (lt['exit'] is None) != (st['exit'] is None) and not pending:
                d['exit_note'] = (f"live {'offen' if lt['exit'] is None else 'zu'}, "
                                  f"Sim {'offen' if st['exit'] is None else 'zu'}")
        else:
            dev = abs(lt['exit'] - st['exit']) / st['exit'] * 100
            if dev > EXIT_TOL_PCT:
                d['exit_dev_pct'] = dev
                d['reason'] = explain_exit(st, lt)
        items.append(d)
    for st in missing:
        items.append(dict(status='fehlt', key=st['key'], name=_short(st['key']), side=st['side'], live=None,
                          sim=st_d(st), exit_dev_pct=None, exit_note='',
                          reason=explain_from_log(st['key'], st['signal_time'], st['side'])))
    for lt, in_team in extra:
        key = in_team[0] if in_team else None
        prev = [t for t in live if t['symbol'] == lt['symbol'] and t['exit_time'] is not None
                and t['exit_time'] <= lt['entry_time'] and t['entry_time'] < lt['entry_time']]
        # Simulator-Exitzeit = Beginn der Kerze, in der der Exit lag
        sim_open = [st for _, st in rows if st['symbol'] == lt['symbol'] and st['signal_time'] < lt['entry_time']
                    and (st['exit_time'] is None or lt['entry_time'] < st['exit_time'] + _tf_dur(st['key']))]
        if not in_team:
            reason = 'Strategie war zu dem Zeitpunkt nicht im Team'
        elif prev and sim_open:
            sim_exit = (f"Exit in Kerze ab {_fmt_t(sim_open[-1]['exit_time'])}" if sim_open[-1]['exit_time']
                        else "dort noch offen")
            reason = (f"Wiedereinstieg: live schon {_fmt_t(prev[-1]['exit_time'])} ausgestiegen, Simulator war "
                      f"noch in der Position ({sim_exit})")
        else:
            reason = explain_from_log(key, lt['entry_time'] - pd.Timedelta(minutes=1), lt['side'])
        items.append(dict(status='nur_live', key=key, name=_short(key) if key else lt['symbol'][:-4],
                          side=lt['side'], live=lt_d(lt), sim=None, exit_dev_pct=None, exit_note='',
                          reason=reason))
    return items


def build_message(items, unknown, order_flags, live, equity, days, team_lines=()):
    same = [d for d in items if d['status'] == 'gleich']
    missing = [d for d in items if d['status'] == 'fehlt']
    extra = [d for d in items if d['status'] == 'nur_live']
    exit_dev = [d for d in same if d['exit_dev_pct'] or d['exit_note']]
    sim_pnl = sum(d['sim']['pnl'] for d in items if d['sim'] and d['sim']['pnl'] is not None)
    live_pnl = sum(t['pnl'] for t in live if t['exit_time'] is not None)
    n_sim = len(same) + len(missing)
    ok = not (extra or missing or exit_dev or order_flags)

    lines = [f"📊 ltbbot Live vs. Simulator (letzte {days} Tage)", ""]
    lines.append(f"Live: {len(live)} Trades | PnL geschlossen {live_pnl:+.2f} USDT")
    lines.append(f"Simulator: {n_sim} Trades | PnL {sim_pnl:+.2f} USDT")
    if equity is not None:
        lines.append(f"Konto: {equity:.2f} USDT")
    lines.append("")
    if team_lines:
        lines += list(team_lines) + [""]
    lines.append(f"✅ Gleiche Einstiege: {len(same)}/{n_sim}" if n_sim else "Keine Simulator-Signale im Team.")
    if missing:
        lines.append(f"\n❗ Fehlt live ({len(missing)}):")
        for d in missing:
            lines.append(f"• {d['name']} {d['side']} Signal {_fmt_t(d['sim']['signal_time'])}")
            lines.append(f"  → {d['reason']}")
    if extra:
        lines.append(f"\n❗ Nur live ({len(extra)}):")
        for d in extra:
            why = 'kein Simulator-Signal' if d['key'] else 'nicht im Team'
            lines.append(f"• {d['name']} {d['side']} {_fmt_t(d['live']['entry_time'])} ({why})")
            lines.append(f"  → {d['reason']}")
    if exit_dev:
        lines.append(f"\n⚠️ Exit-Abweichung > {EXIT_TOL_PCT} % ({len(exit_dev)}):")
        for d in exit_dev:
            if d['exit_note']:
                lines.append(f"• {d['name']} {_fmt_t(d['live']['entry_time'])}: {d['exit_note']}")
            else:
                lines.append(f"• {d['name']} {_fmt_t(d['live']['entry_time'])}: Exit live {d['live']['exit']:g} / "
                             f"Sim {d['sim']['exit']:.6g} ({d['exit_dev_pct']:.1f} %)")
                lines.append(f"  → {d['reason']}")
    if order_flags:
        lines.append(f"\n⚠️ Hinweise ({len(order_flags)}):")
        lines += [f"• {x}" for x in order_flags]
    if unknown:
        lines.append(f"\nℹ️ {len(unknown)} Trade(s) vor Beginn der Teamhistorie, nicht bewertet")
    lines.append("\n" + ("Alles im Gleichlauf." if ok else "Abweichungen prüfen (Ursache vor Fix)."))
    lines.append("\n🖼 Übersicht mit Grafiken: Bild und HTML-Datei unten (antippen zum Öffnen)")
    return "\n".join(lines)


def run_check(days, send, history_path):
    acc, tg = _secrets()
    end = pd.Timestamp.now(tz='UTC').floor('min')
    start = end - pd.Timedelta(days=days)
    live, carry, open_pos, plans, equity = fetch_live(acc, start)

    hist = team_history.load_history(history_path)
    # Kontostand zu Fensterbeginn: heutiges Equity ohne offene uPnL und ohne Ergebnis der Trades im Fenster
    realized_now = (equity or 25.0) - sum(float(p['unrealizedPL']) for p in open_pos)
    start_capital = realized_now - sum(t['pnl'] for t in live if t['exit_time'] is not None)
    sim, data_flags = simulate(hist, start, end, start_capital, carry)
    rows, extra, missing, unknown = compare(live, sim, history_path)
    # Kopfzeile/Uebersicht nur ueber bewertete Trades (gleiches Team bekannt) -- sonst stuende
    # Live mit Trades vor Beginn der Teamhistorie gegen einen Backtest ohne diese Trades
    live_eval = [t for t in live if not any(t is u for u in unknown)]
    items = build_items(rows, extra, missing, live_eval, end)
    order_flags = positions_without_sl(open_pos, plans) + data_flags + check_orders(open_pos, plans)
    team_lines = portfolio_lines(hist, start, end)
    msg = build_message(items, unknown, order_flags, live_eval, equity, days, team_lines)
    print(msg)

    keys = sorted({k for e in hist for k in e['team'] if pd.Timestamp(e['valid_from']) <= end})
    team_periods = {_short(k): [(a, b) for a, b in active_periods(hist, k) if b is None or b > start]
                    for k in keys}
    team_periods = {k: v for k, v in team_periods.items() if v}
    summary = dict(live_n=len(live_eval), live_pnl=sum(t['pnl'] for t in live_eval if t['exit_time'] is not None),
                   sim_pnl=sum(d['sim']['pnl'] for d in items if d['sim'] and d['sim']['pnl'] is not None),
                   equity=equity)
    names = {_symbol_raw(k): _short(k) for k in keys}
    carry_named = [dict(c, name=names.get(c['symbol'], c['symbol'][:-4])) for c in carry]
    png, page = write_report(items, team_periods, team_lines, order_flags, summary, start, end, REPORT_DIR,
                             carry_named)
    print(f"Uebersicht: {png} | {page}")
    if send:
        send_message(tg.get('bot_token'), tg.get('chat_id'), msg)
        send_photo(tg.get('bot_token'), tg.get('chat_id'), png, caption='ltbbot Live vs. Backtest – Übersicht')
        send_document(tg.get('bot_token'), tg.get('chat_id'), page,
                      caption='Interaktive Übersicht: antippen, im Browser öffnen')
        _log(f"GESENDET live={len(live)} match={len(rows)} fehlt={len(missing)} extra={len(extra)}")
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--history', default=os.environ.get('LTBBOT_TEAM_HISTORY', team_history.HISTORY_FILE))
    args = ap.parse_args()
    logging.disable(logging.WARNING)

    cfg = _settings()
    if not args.force:
        if not cfg['enabled'] or not _is_due(cfg['send_hour']):
            return 0
        _set_last_run()
    try:
        run_check(cfg['rolling_window_days'], send=args.send or not args.force, history_path=args.history)
    except Exception as e:
        _log(f"FEHLER {type(e).__name__}: {e}")
        if not args.force:
            acc, tg = _secrets()
            send_message(tg.get('bot_token'), tg.get('chat_id'), f"❌ ltbbot Live-vs-Simulator-Check fehlgeschlagen: {e}")
        raise
    return 0


if __name__ == '__main__':
    sys.exit(main())
