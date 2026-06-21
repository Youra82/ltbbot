#!/usr/bin/env python3
"""
analysis_runner.py — ltbbot Envelope Strategie Analysen

Aufruf via run_analysis.sh oder direkt:
  python3 src/ltbbot/analysis/analysis_runner.py --mode 1 --capital 50 --lookback 365
"""
import argparse
import contextlib
import io
import json
import logging
import os
import sys
from datetime import date, timedelta, datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.analysis.backtester import load_data, run_envelope_backtest
from ltbbot.analysis.portfolio_simulator import run_portfolio_simulation
from ltbbot.utils.telegram import send_message, send_photo

logging.basicConfig(level=logging.ERROR)
logging.getLogger('ltbbot').setLevel(logging.ERROR)

TMP = '/tmp'
DARK_BG = '#0d1117'
C1, C2, C3 = '#2563eb', '#22c55e', '#ef4444'
C4, C5, C6 = '#f59e0b', '#a855f7', '#06b6d4'
PALETTE = [C1, C2, C3, C4, C5, C6]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _load_settings():
    with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
        return json.load(f)


def _load_secrets():
    with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
        return json.load(f)


def _tg(secrets):
    tg = secrets.get('telegram', {})
    return tg.get('bot_token'), tg.get('chat_id')


def _send(token, chat, text, send_telegram=True):
    if send_telegram and token and chat:
        send_message(token, chat, text)
    print(text)


def _send_fig(token, chat, fig, caption, path, send_telegram=True):
    fig.savefig(path, dpi=120, bbox_inches='tight', facecolor=DARK_BG)
    plt.close(fig)
    print(f"  Chart: {path}")
    if send_telegram and token and chat:
        send_photo(token, chat, path, caption)


def _active_strategies(settings):
    return [s for s in settings.get('live_trading_settings', {}).get('active_strategies', [])
            if s.get('active', True)]


def _load_config(symbol, timeframe):
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    for name in [f'config_{safe}_envelope.json', f'config_{safe}.json']:
        path = os.path.join(configs_dir, name)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return {
        'market':   {'symbol': symbol, 'timeframe': timeframe},
        'strategy': {'average_type': 'EMA', 'average_period': 20, 'envelopes': [0.03, 0.06, 0.09]},
        'risk':     {'stop_loss_pct': 3.0, 'leverage': 10},
        'behavior': {},
    }


def _build_strategies_data(strategies, start_date, end_date):
    data = {}
    for s in strategies:
        sym, tf = s['symbol'], s['timeframe']
        df = load_data(sym, tf, start_date, end_date)
        if df is None or len(df) < 30:
            print(f"  SKIP {sym}/{tf} — zu wenig Daten")
            continue
        cfg = _load_config(sym, tf)
        key = f"{sym.split('/')[0]}_{tf}"
        data[key] = {'symbol': sym, 'timeframe': tf, 'data': df, 'params': cfg}
    return data


def _run_portfolio_sim_quiet(capital, strategies_data, start_date, end_date):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return run_portfolio_simulation(capital, strategies_data, start_date, end_date)


def _dark_fig(w=12, h=6):
    fig = plt.figure(figsize=(w, h), facecolor=DARK_BG)
    return fig


def _style_ax(ax):
    ax.set_facecolor('#161b22')
    for spine in ax.spines.values():
        spine.set_color('#30363d')
    ax.tick_params(colors='#8b949e', labelsize=8)
    ax.xaxis.label.set_color('#c9d1d9')
    ax.yaxis.label.set_color('#c9d1d9')
    ax.title.set_color('#f0f6fc')
    ax.grid(True, color='#21262d', linewidth=0.5, alpha=0.7)
    return ax


def _calmar(pnl_pct, dd_pct):
    return pnl_pct / dd_pct if dd_pct > 0 else 0.0


# ─── Analyse 1: Walk-Forward Lookback ────────────────────────────────────────

def analyse_walkforward_lookback(capital, min_trades, send_telegram, token, chat):
    _send(token, chat, "ltbbot Walk-Forward Lookback-Analyse gestartet...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        _send(token, chat, "Keine aktiven Strategien.", send_telegram)
        return

    lookbacks = [1, 2, 4, 8, 12, 26]
    end_date  = date.today()
    max_lb    = max(lookbacks)
    full_days = max_lb * 7 + 52 * 7
    start_date = end_date - timedelta(days=full_days)

    print(f"  Lade Daten {start_date} → {end_date}...")
    pairs_data = {}
    for s in strategies:
        sym, tf = s['symbol'], s['timeframe']
        df = load_data(sym, tf, str(start_date), str(end_date))
        if df is not None and len(df) > 50:
            cfg = _load_config(sym, tf)
            pairs_data[f"{sym.split('/')[0]}_{tf}"] = {'symbol': sym, 'timeframe': tf, 'df': df, 'cfg': cfg}

    if not pairs_data:
        _send(token, chat, "Keine Daten geladen.", send_telegram)
        return

    # Erstelle Liste aller OOS-Wochen
    all_mondays = pd.date_range(
        start=pd.Timestamp(start_date) + timedelta(weeks=max_lb + 1),
        end=pd.Timestamp(end_date),
        freq='W-MON', tz='UTC'
    )

    from tqdm import tqdm
    results = {}
    for lb in lookbacks:
        eq = capital
        equity_series = [eq]
        empty_weeks = 0

        for oos_start in tqdm(all_mondays, desc=f"  Lookback {lb:>2}W", unit="Woche", leave=True):
            oos_end = oos_start + timedelta(days=7)
            is_end  = oos_start
            is_start = is_end - timedelta(weeks=lb)

            scored = []
            for key, pd_info in pairs_data.items():
                df_is = pd_info['df'].loc[
                    (pd_info['df'].index >= is_start) & (pd_info['df'].index < is_end)
                ]
                if len(df_is) < max(min_trades, 10):
                    continue
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        r = run_envelope_backtest(df_is, pd_info['cfg'], start_capital=capital, show_progress=False)
                    if r and r.get('trades_count', 0) >= min_trades and r.get('max_drawdown_pct', 100) > 0:
                        c = _calmar(r['total_pnl_pct'], r['max_drawdown_pct'])
                        if c > 0:
                            scored.append((c, key, pd_info))
                except Exception:
                    pass

            if not scored:
                empty_weeks += 1
                equity_series.append(eq)
                continue

            scored.sort(reverse=True)
            best = scored[0][2]

            df_oos = best['df'].loc[
                (best['df'].index >= oos_start) & (best['df'].index < oos_end)
            ]
            if len(df_oos) < 5:
                equity_series.append(eq)
                continue

            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    r_oos = run_envelope_backtest(df_oos, best['cfg'], start_capital=eq, show_progress=False)
                if r_oos:
                    eq = r_oos['end_capital']
            except Exception:
                pass
            equity_series.append(eq)

        arr    = np.array(equity_series)
        dates  = [pd.Timestamp(start_date) + timedelta(weeks=max_lb + 1) + timedelta(weeks=i)
                  for i in range(len(arr))]
        peak   = np.maximum.accumulate(arr)
        dd     = (peak - arr) / peak * 100
        max_dd = float(dd.max()) if len(dd) > 0 else 0
        total_pnl = (eq - capital) / capital * 100
        calmar = _calmar(total_pnl, max_dd)
        results[lb] = {'equity': arr, 'dates': dates, 'calmar': calmar,
                       'dd': max_dd, 'pnl': total_pnl, 'empty': empty_weeks}

    best_lb     = max(results, key=lambda x: results[x]['calmar'])
    best_calmar = results[best_lb]['calmar']
    n_oos       = len(all_mondays)

    # ── Chart: 2 Subplots (Equity oben, Calmar-Balken unten) ─────────────────
    fig = _dark_fig(14, 10)
    gs  = gridspec.GridSpec(2, 1, figure=fig, height_ratios=[2, 1], hspace=0.4)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    _style_ax(ax1)
    _style_ax(ax2)

    # Equity-Kurven (Log-Skala)
    for i, (lb, r) in enumerate(results.items()):
        sign  = '+' if r['pnl'] >= 0 else ''
        label = (f"{lb}W Lookback: {sign}{r['pnl']:.1f}% | "
                 f"DD {r['dd']:.1f}% | Calmar {r['calmar']:.1f}")
        lw    = 2.2 if lb == best_lb else 1.2
        alpha = 1.0 if lb == best_lb else 0.75
        ax1.plot(r['dates'], r['equity'], label=label,
                 color=PALETTE[i % len(PALETTE)], linewidth=lw, alpha=alpha)

    ax1.axhline(capital, color='#8b949e', linewidth=0.8, linestyle='--',
                label=f'Start {capital:.0f} USDT')
    ax1.set_yscale('log')
    ax1.set_title(
        f'ltbbot Walk-Forward — Lookback-Vergleich (Out-of-Sample)\n'
        f'Startkapital: {capital:.0f} USDT | Test-Wochen: {n_oos}',
        fontsize=11, pad=10)
    ax1.set_ylabel('Equity (USDT, log)')
    ax1.legend(fontsize=8, facecolor='#161b22', labelcolor='#c9d1d9', loc='upper left')

    # Datum-Ticks: jedes Quartal
    import matplotlib.dates as mdates
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=7)

    # Calmar-Balken
    lbs     = list(results.keys())
    calmars = [results[lb]['calmar'] for lb in lbs]
    colors  = [C4 if lb == best_lb else PALETTE[i % len(PALETTE)]
               for i, lb in enumerate(lbs)]
    bars    = ax2.bar([f"{lb}W" for lb in lbs], calmars, color=colors, width=0.6)

    for bar, val, lb in zip(bars, calmars, lbs):
        ax2.text(bar.get_x() + bar.get_width() / 2,
                 bar.get_height() + max(abs(v) for v in calmars) * 0.02,
                 f'{val:.1f}', ha='center', va='bottom',
                 color='#f0f6fc', fontsize=9, fontweight='bold')
        if lb == best_lb:
            ax2.text(bar.get_x() + bar.get_width() / 2,
                     min(0, bar.get_height()) - max(abs(v) for v in calmars) * 0.06,
                     '★ BEST', ha='center', va='top',
                     color=C4, fontsize=9, fontweight='bold')

    ax2.axhline(0, color='#8b949e', linewidth=0.8)
    ax2.set_title('Calmar Score pro Lookback (Out-of-Sample, höher = besser)', fontsize=10)
    ax2.set_xlabel('Lookback-Zeitraum')
    ax2.set_ylabel('Calmar Score (OOS)')

    path = f'{TMP}/ltbbot_wf_lookback.png'
    _send_fig(token, chat, fig, 'ltbbot Walk-Forward Lookback', path, send_telegram)

    # Text-Tabelle
    lines = [f'ltbbot Walk-Forward Lookback-Vergleich\nStartkapital: {capital:.0f} USDT | Test-Wochen: {n_oos}\n']
    for lb, r in results.items():
        star = ' ← bestes Calmar' if lb == best_lb else ''
        sign = '+' if r['pnl'] >= 0 else ''
        lines.append(
            f"Lookback {lb:>2}W  PnL={sign}{r['pnl']:6.1f}% | "
            f"DD={r['dd']:5.1f}% | Calmar={r['calmar']:7.1f} | "
            f"Leerwochen={r['empty']:2d}{star}"
        )
    _send(token, chat, '\n'.join(lines), send_telegram)

    # Bestes Ergebnis in settings.json schreiben
    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
            s = json.load(f)
        s.setdefault('optimization_settings', {})['backtest_lookback_weeks'] = best_lb
        s['optimization_settings']['_backtest_lookback_note'] = (
            'Wie viele Wochen zurueck der Portfolio-Optimizer schaut. '
            'Optimal per run_analysis.sh Analyse 1 ermittelt.'
        )
        with open(os.path.join(PROJECT_ROOT, 'settings.json'), 'w') as f:
            json.dump(s, f, indent=4)
        print(f"  settings.json: backtest_lookback_weeks = {best_lb} gesetzt.")
    except Exception as e:
        print(f"  WARNUNG: settings.json konnte nicht aktualisiert werden: {e}")


# ─── Analyse 2: Envelope Parameter Walk-Forward ───────────────────────────────

def analyse_param_walkforward(capital, send_telegram, token, chat):
    _send(token, chat, "ltbbot Envelope Parameter Walk-Forward gestartet...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    s    = strategies[0]
    sym, tf = s['symbol'], s['timeframe']
    cfg_base = _load_config(sym, tf)
    base_sl  = cfg_base.get('risk', {}).get('stop_loss_pct', 3.0)
    base_per = cfg_base.get('strategy', {}).get('average_period', 20)

    end_date   = date.today()
    start_date = end_date - timedelta(days=730)
    df = load_data(sym, tf, str(start_date), str(end_date))
    if df is None or len(df) < 50:
        _send(token, chat, f"Zu wenig Daten für {sym}/{tf}", send_telegram)
        return

    oos_weeks = pd.date_range(
        start=pd.Timestamp(start_date) + timedelta(weeks=9),
        end=pd.Timestamp(end_date), freq='W-MON', tz='UTC'
    )

    param_sets = {
        f"SL={sl:.1f}%": {**cfg_base, 'risk': {**cfg_base.get('risk', {}), 'stop_loss_pct': sl}}
        for sl in [round(base_sl * m, 2) for m in [0.5, 0.75, 1.0, 1.5, 2.0]]
    }

    from tqdm import tqdm
    results = {}
    for label, cfg in param_sets.items():
        eq = capital
        equity_series = [eq]
        for oos_start in tqdm(oos_weeks, desc=f"  {label}", unit="Woche", leave=True):
            oos_end = oos_start + timedelta(days=7)
            df_oos  = df.loc[(df.index >= oos_start) & (df.index < oos_end)]
            if len(df_oos) < 5:
                equity_series.append(eq)
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    r = run_envelope_backtest(df_oos, cfg, start_capital=eq, show_progress=False)
                if r:
                    eq = r['end_capital']
            except Exception:
                pass
            equity_series.append(eq)
        arr    = np.array(equity_series)
        peak   = np.maximum.accumulate(arr)
        max_dd = float(((peak - arr) / peak * 100).max()) if len(arr) > 1 else 0
        pnl    = (eq - capital) / capital * 100
        results[label] = {'equity': arr, 'calmar': _calmar(pnl, max_dd), 'dd': max_dd, 'pnl': pnl}

    fig = _dark_fig(14, 6)
    ax  = fig.add_subplot(111)
    _style_ax(ax)
    ax.set_title(f'Envelope SL Walk-Forward — {sym.split("/")[0]}/{tf}', fontsize=13, pad=12)
    for i, (label, r) in enumerate(results.items()):
        ax.plot(r['equity'], label=f"{label} | Cal={r['calmar']:.1f} | DD={r['dd']:.1f}%",
                color=PALETTE[i % len(PALETTE)], linewidth=1.5)
    ax.axhline(capital, color='#8b949e', linewidth=0.8, linestyle='--')
    ax.legend(fontsize=8, facecolor='#161b22', labelcolor='#c9d1d9')
    ax.set_xlabel('Wochen (OOS)')
    ax.set_ylabel('Equity (USDT)')
    fig.tight_layout()
    path = f'{TMP}/ltbbot_param_wf.png'
    _send_fig(token, chat, fig, 'ltbbot Envelope Parameter Walk-Forward', path, send_telegram)

    lines = [f'ltbbot Envelope SL-Sweep Walk-Forward ({sym.split("/")[0]}/{tf})\n']
    best = max(results.values(), key=lambda x: x['calmar'])
    for label, r in results.items():
        star = ' ← bestes Calmar' if r is best else ''
        lines.append(f"{label:<12} PnL={r['pnl']:+6.1f}% | DD={r['dd']:5.1f}% | Calmar={r['calmar']:7.1f}{star}")
    _send(token, chat, '\n'.join(lines), send_telegram)


# ─── Analyse 3: Slippage & Fee Impact ────────────────────────────────────────

def analyse_fee_impact(capital, lookback_days, send_telegram, token, chat):
    _send(token, chat, "ltbbot Slippage & Fee Impact Analyse...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=lookback_days))
    sd = _build_strategies_data(strategies, start_date, end_date)
    if not sd:
        return

    result = _run_portfolio_sim_quiet(capital, sd, start_date, end_date)
    if not result:
        return

    trades = result.get('trades_df')
    if trades is None or len(trades) == 0:
        _send(token, chat, "Keine Trades für Fee-Analyse.", send_telegram)
        return

    fee_rates = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]  # % per Seite
    slip_rates = [0.0, 0.02, 0.05, 0.10, 0.15, 0.20]

    def recalc_pnl(df, extra_fee_pct, extra_slip_pct):
        total = 0.0
        wins  = 0
        for _, t in df.iterrows():
            notional = abs(float(t.get('amount_coins', 0)) * float(t.get('entry_price', 0)))
            fee_cost  = notional * (extra_fee_pct / 100) * 2
            slip_cost = notional * (extra_slip_pct / 100) if t.get('reason') == 'sl' else 0
            adjusted  = float(t.get('pnl_usd', 0)) - fee_cost - slip_cost
            total    += adjusted
            if adjusted > 0:
                wins  += 1
        return total, wins / max(len(df), 1) * 100

    # Fee sweep
    fee_pnls = []
    base_pnl = float(trades['pnl_usd'].sum())
    for f in fee_rates:
        pnl, _ = recalc_pnl(trades, f, 0)
        fee_pnls.append(pnl)

    breakeven_fee = None
    for i in range(len(fee_rates) - 1):
        if fee_pnls[i] >= 0 and fee_pnls[i + 1] < 0:
            t = fee_pnls[i] / (fee_pnls[i] - fee_pnls[i + 1])
            breakeven_fee = fee_rates[i] + t * (fee_rates[i + 1] - fee_rates[i])

    slip_pnls = []
    for s in slip_rates:
        pnl, _ = recalc_pnl(trades, 0.06, s)
        slip_pnls.append(pnl)

    fig = _dark_fig(14, 5)
    gs  = gridspec.GridSpec(1, 2, figure=fig, wspace=0.35)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    _style_ax(ax1)
    _style_ax(ax2)

    colors1 = [C2 if v >= 0 else C3 for v in fee_pnls]
    ax1.bar([f"{f:.2f}%" for f in fee_rates], fee_pnls, color=colors1)
    ax1.axhline(0, color='#8b949e', linewidth=0.8)
    ax1.set_title('Fee Impact (PnL USDT)', fontsize=11)
    ax1.set_xlabel('Fee/Seite')
    ax1.set_ylabel('PnL (USDT)')
    if breakeven_fee:
        ax1.axvline(x=fee_rates.index(min(fee_rates, key=lambda x: abs(x - breakeven_fee))),
                    color=C4, linestyle='--', linewidth=1, label=f'Break-Even ~{breakeven_fee:.2f}%')
        ax1.legend(fontsize=8, facecolor='#161b22', labelcolor='#c9d1d9')

    colors2 = [C2 if v >= 0 else C3 for v in slip_pnls]
    ax2.bar([f"{s:.2f}%" for s in slip_rates], slip_pnls, color=colors2)
    ax2.axhline(0, color='#8b949e', linewidth=0.8)
    ax2.set_title('Slippage Impact (Fee fix 0.06%/Seite)', fontsize=11)
    ax2.set_xlabel('SL-Slippage')
    ax2.set_ylabel('PnL (USDT)')
    fig.suptitle('ltbbot Fee & Slippage Impact', color='#f0f6fc', fontsize=13)
    fig.tight_layout()
    path = f'{TMP}/ltbbot_fee_impact.png'
    _send_fig(token, chat, fig, 'ltbbot Fee & Slippage', path, send_telegram)

    be_str = f"{breakeven_fee:.2f}%/Seite" if breakeven_fee else ">0.20% (sehr robust)"
    msg = (
        f"ltbbot Fee & Slippage Impact\n"
        f"Trades: {len(trades)} | Basis-PnL: {base_pnl:+.2f} USDT\n"
        f"Break-Even Fee: {be_str}\n"
        f"Bitget Taker: 0.06%/Seite → PnL: {recalc_pnl(trades, 0.06, 0)[0]:+.2f} USDT\n"
        f"Mit 0.10% Slippage: {recalc_pnl(trades, 0.06, 0.10)[0]:+.2f} USDT"
    )
    _send(token, chat, msg, send_telegram)


# ─── Analyse 4: Monte Carlo ───────────────────────────────────────────────────

def analyse_monte_carlo(capital, lookback_days, n_sims, send_telegram, token, chat):
    _send(token, chat, f"ltbbot Monte Carlo ({n_sims:,} Sim.)...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=lookback_days))
    sd = _build_strategies_data(strategies, start_date, end_date)
    if not sd:
        return

    result = _run_portfolio_sim_quiet(capital, sd, start_date, end_date)
    if not result:
        return

    trades = result.get('trades_df')
    if trades is None or len(trades) < 5:
        _send(token, chat, "Zu wenig Trades für Monte Carlo.", send_telegram)
        return

    pnls = trades['pnl_usd'].values.astype(float)
    rng  = np.random.default_rng(42)
    final_equities = []
    max_dds        = []
    ruin_count     = 0

    for _ in range(n_sims):
        shuffled = rng.permutation(pnls)
        eq = np.concatenate([[capital], capital + np.cumsum(shuffled)])
        peak  = np.maximum.accumulate(eq)
        dd    = (peak - eq) / peak * 100
        final_equities.append(float(eq[-1]))
        max_dds.append(float(dd.max()))
        if eq[-1] < capital * 0.5:
            ruin_count += 1

    fe  = np.array(final_equities)
    mdd = np.array(max_dds)
    p5, p50, p95 = np.percentile(fe, [5, 50, 95])
    ruin_pct = ruin_count / n_sims * 100

    fig = _dark_fig(14, 5)
    gs  = gridspec.GridSpec(1, 2, figure=fig, wspace=0.35)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    _style_ax(ax1)
    _style_ax(ax2)

    ax1.hist(fe, bins=60, color=C1, alpha=0.8, edgecolor='none')
    ax1.axvline(p5,  color=C3, linewidth=1.5, linestyle='--', label=f'5.  Pzl: {p5:.1f}')
    ax1.axvline(p50, color=C4, linewidth=1.5, linestyle='--', label=f'50. Pzl: {p50:.1f}')
    ax1.axvline(p95, color=C2, linewidth=1.5, linestyle='--', label=f'95. Pzl: {p95:.1f}')
    ax1.axvline(capital, color='#8b949e', linewidth=1, linestyle=':')
    ax1.set_title('Final-Equity Verteilung', fontsize=11)
    ax1.set_xlabel('Endkapital (USDT)')
    ax1.legend(fontsize=8, facecolor='#161b22', labelcolor='#c9d1d9')

    mdd5, mdd50, mdd95 = np.percentile(mdd, [5, 50, 95])
    ax2.hist(mdd, bins=60, color=C3, alpha=0.8, edgecolor='none')
    ax2.axvline(mdd50, color=C4, linewidth=1.5, linestyle='--', label=f'Median: {mdd50:.1f}%')
    ax2.axvline(mdd95, color=C3, linewidth=1.5, linestyle='--', label=f'95. Pzl: {mdd95:.1f}%')
    ax2.set_title('Max. Drawdown Verteilung', fontsize=11)
    ax2.set_xlabel('Max. Drawdown (%)')
    ax2.legend(fontsize=8, facecolor='#161b22', labelcolor='#c9d1d9')

    fig.suptitle(f'ltbbot Monte Carlo ({n_sims:,} Simulationen)', color='#f0f6fc', fontsize=13)
    fig.tight_layout()
    path = f'{TMP}/ltbbot_monte_carlo.png'
    _send_fig(token, chat, fig, 'ltbbot Monte Carlo', path, send_telegram)

    msg = (
        f"ltbbot Monte Carlo ({n_sims:,} Sim.)\n"
        f"Trades: {len(pnls)} | Startkapital: {capital:.0f} USDT\n\n"
        f"Final-Equity:\n"
        f"  5. Pzl:  {p5:.2f} USDT ({(p5-capital)/capital*100:+.1f}%)\n"
        f"  Median:  {p50:.2f} USDT ({(p50-capital)/capital*100:+.1f}%)\n"
        f"  95. Pzl: {p95:.2f} USDT ({(p95-capital)/capital*100:+.1f}%)\n\n"
        f"Max. Drawdown Median: {mdd50:.1f}%\n"
        f"Max. Drawdown 95.Pzl: {mdd95:.1f}%\n"
        f"Ruin-Wahrscheinlichkeit (<50% Kapital): {ruin_pct:.2f}%"
    )
    _send(token, chat, msg, send_telegram)


# ─── Analyse 5: Anti-Korrelations-Portfolio ───────────────────────────────────

def analyse_correlation(capital, lookback_days, send_telegram, token, chat):
    _send(token, chat, "ltbbot Anti-Korrelations-Portfolio...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=lookback_days))
    sd = _build_strategies_data(strategies, start_date, end_date)
    if not sd:
        return

    result = _run_portfolio_sim_quiet(capital, sd, start_date, end_date)
    if not result:
        return

    trades = result.get('trades_df')
    if trades is None or len(trades) < 5:
        _send(token, chat, "Zu wenig Trades.", send_telegram)
        return

    trades = trades.copy()
    trades['week'] = pd.to_datetime(trades['exit_time']).dt.to_period('W')
    weekly = trades.groupby(['strategy_id', 'week'])['pnl_usd'].sum().unstack(0).fillna(0)

    if weekly.shape[1] < 2:
        _send(token, chat, "Weniger als 2 Pairs — kein Korrelationsvergleich möglich.", send_telegram)
        return

    corr = weekly.corr()
    labels = [c.replace('_', '/') for c in corr.columns]

    fig = _dark_fig(10, 8)
    ax  = fig.add_subplot(111)
    _style_ax(ax)
    im = ax.imshow(corr.values, cmap='RdYlGn', vmin=-1, vmax=1, aspect='auto')
    ax.set_xticks(range(len(labels)))
    ax.set_yticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=9)
    ax.set_yticklabels(labels, fontsize=9)
    for i in range(len(labels)):
        for j in range(len(labels)):
            ax.text(j, i, f"{corr.values[i,j]:.2f}", ha='center', va='center',
                    fontsize=8, color='#0d1117')
    plt.colorbar(im, ax=ax)
    ax.set_title('Weekly PnL Korrelationsmatrix', fontsize=12, pad=12)
    fig.tight_layout()
    path = f'{TMP}/ltbbot_correlation.png'
    _send_fig(token, chat, fig, 'ltbbot Korrelationsmatrix', path, send_telegram)

    pairs_list = [(corr.columns[i], corr.columns[j], corr.values[i,j])
                  for i in range(len(corr)) for j in range(i+1, len(corr))]
    pairs_list.sort(key=lambda x: x[2])
    lines = ['ltbbot Pair-Korrelationen (wöchentlich)\n']
    lines.append('Beste Anti-Korrelation (Diversifikation):')
    for a, b, v in pairs_list[:3]:
        lines.append(f"  {a} ↔ {b}: {v:+.3f}")
    lines.append('\nHöchste Korrelation (kein Div.-Vorteil):')
    for a, b, v in pairs_list[-3:]:
        lines.append(f"  {a} ↔ {b}: {v:+.3f}")
    _send(token, chat, '\n'.join(lines), send_telegram)


# ─── Analyse 6: Kelly Position Sizing ────────────────────────────────────────

def analyse_kelly(capital, lookback_days, send_telegram, token, chat):
    _send(token, chat, "ltbbot Kelly Position Sizing...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=lookback_days))
    sd = _build_strategies_data(strategies, start_date, end_date)
    if not sd:
        return

    result = _run_portfolio_sim_quiet(capital, sd, start_date, end_date)
    if not result:
        return

    trades = result.get('trades_df')
    if trades is None or len(trades) < 5:
        _send(token, chat, "Zu wenig Trades.", send_telegram)
        return

    lines = ['ltbbot Kelly Position Sizing\n']
    for key, info in sd.items():
        pair_trades = trades[trades['strategy_id'] == key] if 'strategy_id' in trades.columns else trades
        if len(pair_trades) < 3:
            continue
        wins  = pair_trades[pair_trades['pnl_usd'] > 0]['pnl_usd']
        losses = pair_trades[pair_trades['pnl_usd'] <= 0]['pnl_usd']
        wr    = len(wins) / len(pair_trades)
        avg_w = float(wins.mean()) if len(wins) > 0 else 0
        avg_l = abs(float(losses.mean())) if len(losses) > 0 else 1
        rr    = avg_w / avg_l if avg_l > 0 else 1
        kelly = (wr * rr - (1 - wr)) / rr if rr > 0 else 0
        half_kelly = max(0, kelly / 2) * 100
        current_risk = info['params'].get('risk', {}).get('stop_loss_pct', 3.0)
        lines.append(
            f"{info['symbol'].split('/')[0]}/{info['timeframe']}\n"
            f"  WR: {wr*100:.1f}% | RR: {rr:.2f}:1 | Kelly: {kelly*100:.1f}% | "
            f"Half-Kelly: {half_kelly:.1f}% | Aktuell SL: {current_risk:.1f}%"
        )

    _send(token, chat, '\n'.join(lines), send_telegram)


# ─── Analyse 7: Regime Performance ───────────────────────────────────────────

def analyse_regime(capital, lookback_days, send_telegram, token, chat):
    _send(token, chat, "ltbbot Regime Performance Analyse...", send_telegram)
    from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=lookback_days))
    sd = _build_strategies_data(strategies, start_date, end_date)
    if not sd:
        return

    result = _run_portfolio_sim_quiet(capital, sd, start_date, end_date)
    if not result:
        return

    trades = result.get('trades_df')
    if trades is None or len(trades) < 5:
        _send(token, chat, "Zu wenig Trades.", send_telegram)
        return

    # Berechne Regime für jeden Trade basierend auf entry_time
    regimes = []
    for key, info in sd.items():
        pair_trades = trades[trades['strategy_id'] == key] if 'strategy_id' in trades.columns else trades
        df_ind, _ = calculate_indicators_and_signals(info['data'], info['params'])
        for _, t in pair_trades.iterrows():
            entry_t = pd.to_datetime(t['entry_time'])
            idx = df_ind.index.get_indexer([entry_t], method='nearest')[0]
            if idx >= 0 and 'regime' in df_ind.columns:
                regime = df_ind.iloc[idx].get('regime', 'UNKNOWN')
            else:
                regime = 'UNKNOWN'
            regimes.append({'regime': regime, 'pnl': float(t['pnl_usd']), 'win': float(t['pnl_usd']) > 0})

    df_r = pd.DataFrame(regimes)
    if df_r.empty:
        _send(token, chat, "Keine Regime-Daten ermittelt.", send_telegram)
        return

    grouped = df_r.groupby('regime').agg(
        trades=('pnl', 'count'), win_rate=('win', 'mean'), total_pnl=('pnl', 'sum')
    ).reset_index()

    fig = _dark_fig(12, 5)
    gs  = gridspec.GridSpec(1, 2, figure=fig, wspace=0.4)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    _style_ax(ax1)
    _style_ax(ax2)

    colors_r = [C2 if v > 0.5 else C3 for v in grouped['win_rate']]
    ax1.bar(grouped['regime'], grouped['win_rate'] * 100, color=colors_r)
    ax1.axhline(50, color='#8b949e', linewidth=0.8, linestyle='--')
    ax1.set_title('Win-Rate nach Regime', fontsize=11)
    ax1.set_ylabel('Win-Rate (%)')

    colors_p = [C2 if v > 0 else C3 for v in grouped['total_pnl']]
    ax2.bar(grouped['regime'], grouped['total_pnl'], color=colors_p)
    ax2.axhline(0, color='#8b949e', linewidth=0.8)
    ax2.set_title('Total PnL nach Regime', fontsize=11)
    ax2.set_ylabel('PnL (USDT)')

    fig.suptitle('ltbbot Regime Performance', color='#f0f6fc', fontsize=13)
    fig.tight_layout()
    path = f'{TMP}/ltbbot_regime.png'
    _send_fig(token, chat, fig, 'ltbbot Regime Performance', path, send_telegram)

    lines = ['ltbbot Regime Performance\n']
    for _, row in grouped.iterrows():
        lines.append(f"{row['regime']:<14} Trades={row['trades']:3d} | WR={row['win_rate']*100:5.1f}% | PnL={row['total_pnl']:+7.2f} USDT")
    _send(token, chat, '\n'.join(lines), send_telegram)


# ─── Analyse 8: Tageszeit-Analyse ────────────────────────────────────────────

def analyse_time_of_day(capital, lookback_days, send_telegram, token, chat):
    _send(token, chat, "ltbbot Tageszeit-Analyse...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=lookback_days))
    sd = _build_strategies_data(strategies, start_date, end_date)
    if not sd:
        return

    result = _run_portfolio_sim_quiet(capital, sd, start_date, end_date)
    if not result:
        return

    trades = result.get('trades_df')
    if trades is None or len(trades) < 5:
        _send(token, chat, "Zu wenig Trades.", send_telegram)
        return

    trades = trades.copy()
    trades['hour'] = pd.to_datetime(trades['entry_time']).dt.hour
    trades['win']  = trades['pnl_usd'] > 0

    by_hour = trades.groupby('hour').agg(count=('win', 'count'), wr=('win', 'mean'), pnl=('pnl_usd', 'sum'))
    all_hours = pd.DataFrame({'hour': range(24)}).set_index('hour')
    by_hour = all_hours.join(by_hour).fillna(0)

    sessions = {'Asia\n01-09': (1, 9), 'Europe\n09-17': (9, 17), 'US\n17-01': (17, 24)}
    sess_stats = {}
    for name, (h1, h2) in sessions.items():
        sub = trades[trades['hour'].between(h1, h2 - 1)]
        if len(sub) > 0:
            sess_stats[name] = {'wr': sub['win'].mean() * 100, 'pnl': sub['pnl_usd'].sum(), 'n': len(sub)}

    fig = _dark_fig(14, 5)
    gs  = gridspec.GridSpec(1, 2, figure=fig, wspace=0.35)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    _style_ax(ax1)
    _style_ax(ax2)

    wr_vals = by_hour['wr'].values * 100
    colors  = [C2 if v > 50 else C3 for v in wr_vals]
    ax1.bar(range(24), wr_vals, color=colors)
    ax1.axhline(50, color='#8b949e', linewidth=0.8, linestyle='--')
    ax1.set_title('Win-Rate nach Einstiegs-Stunde (UTC)', fontsize=11)
    ax1.set_xlabel('Stunde (UTC)')
    ax1.set_ylabel('Win-Rate (%)')
    ax1.set_xticks(range(0, 24, 3))

    pnl_vals = by_hour['pnl'].values
    colors2  = [C2 if v > 0 else C3 for v in pnl_vals]
    ax2.bar(range(24), pnl_vals, color=colors2)
    ax2.axhline(0, color='#8b949e', linewidth=0.8)
    ax2.set_title('PnL nach Einstiegs-Stunde (UTC)', fontsize=11)
    ax2.set_xlabel('Stunde (UTC)')
    ax2.set_ylabel('PnL (USDT)')
    ax2.set_xticks(range(0, 24, 3))

    fig.suptitle('ltbbot Tageszeit-Analyse', color='#f0f6fc', fontsize=13)
    fig.tight_layout()
    path = f'{TMP}/ltbbot_time_of_day.png'
    _send_fig(token, chat, fig, 'ltbbot Tageszeit', path, send_telegram)

    lines = ['ltbbot Tageszeit-Analyse (Session-Überblick)\n']
    for name, stats in sess_stats.items():
        name_clean = name.replace('\n', ' ')
        lines.append(f"{name_clean:<14} WR={stats['wr']:.1f}% | PnL={stats['pnl']:+.2f} USDT | n={stats['n']}")
    _send(token, chat, '\n'.join(lines), send_telegram)


# ─── Analyse 9: Drawdown Duration ────────────────────────────────────────────

def analyse_drawdown_duration(capital, lookback_days, send_telegram, token, chat):
    _send(token, chat, "ltbbot Drawdown Duration Analyse...", send_telegram)
    settings   = _load_settings()
    strategies = _active_strategies(settings)
    if not strategies:
        return

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=lookback_days))
    sd = _build_strategies_data(strategies, start_date, end_date)
    if not sd:
        return

    result = _run_portfolio_sim_quiet(capital, sd, start_date, end_date)
    if not result:
        return

    eq_df = result.get('equity_curve')
    if eq_df is None or (hasattr(eq_df, 'empty') and eq_df.empty):
        _send(token, chat, "Keine Equity-Kurve.", send_telegram)
        return

    equity = eq_df['equity'].values.astype(float)
    times  = eq_df.index if hasattr(eq_df, 'index') else range(len(equity))

    # Drawdown-Perioden extrahieren
    peak_val = equity[0]
    in_dd    = False
    dd_start = None
    dd_peak  = peak_val
    periods  = []

    for i, e in enumerate(equity):
        if e >= peak_val:
            if in_dd and dd_start is not None:
                depth = (dd_peak - min(equity[dd_start:i])) / dd_peak * 100
                periods.append({'start': dd_start, 'end': i, 'depth': depth,
                                 'duration': i - dd_start})
                in_dd = False
            peak_val = e
            dd_peak  = e
        else:
            if not in_dd:
                dd_start = i
                dd_peak  = peak_val
                in_dd    = True

    if not periods:
        _send(token, chat, "Keine Drawdown-Perioden gefunden — konstant profitabel!", send_telegram)
        return

    durations = [p['duration'] for p in periods]
    depths    = [p['depth']    for p in periods]

    fig = _dark_fig(14, 8)
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.35)
    ax1 = fig.add_subplot(gs[0, :])
    ax2 = fig.add_subplot(gs[1, 0])
    ax3 = fig.add_subplot(gs[1, 1])
    for ax in [ax1, ax2, ax3]:
        _style_ax(ax)

    ax1.plot(equity, color=C1, linewidth=1.2, label='Equity')
    for p in periods:
        ax1.axvspan(p['start'], p['end'], alpha=0.15, color=C3)
    ax1.axhline(capital, color='#8b949e', linewidth=0.8, linestyle='--')
    ax1.set_title('Equity-Kurve mit Drawdown-Zonen (rot)', fontsize=11)
    ax1.set_ylabel('Equity (USDT)')
    ax1.legend(fontsize=8, facecolor='#161b22', labelcolor='#c9d1d9')

    ax2.hist(durations, bins=min(20, len(durations)), color=C4, alpha=0.8)
    ax2.set_title('Erholungsdauer (Candles)', fontsize=11)
    ax2.set_xlabel('Dauer (Candles)')
    ax2.axvline(np.mean(durations), color=C2, linewidth=1.5, linestyle='--',
                label=f'Ø {np.mean(durations):.0f}')
    ax2.legend(fontsize=8, facecolor='#161b22', labelcolor='#c9d1d9')

    ax3.scatter(durations, depths, color=C5, alpha=0.7, s=30)
    ax3.set_title('DD-Tiefe vs. Erholungsdauer', fontsize=11)
    ax3.set_xlabel('Dauer (Candles)')
    ax3.set_ylabel('Tiefe (%)')

    fig.suptitle('ltbbot Drawdown Duration Analyse', color='#f0f6fc', fontsize=13)
    fig.tight_layout()
    path = f'{TMP}/ltbbot_drawdown_duration.png'
    _send_fig(token, chat, fig, 'ltbbot Drawdown Duration', path, send_telegram)

    p90 = np.percentile(durations, 90)
    msg = (
        f"ltbbot Drawdown Duration\n"
        f"Perioden: {len(periods)}\n"
        f"Ø Erholungsdauer: {np.mean(durations):.0f} Candles\n"
        f"90. Perzentil:    {p90:.0f} Candles\n"
        f"Längste:          {max(durations):.0f} Candles\n"
        f"Tiefste DD:       {max(depths):.1f}%\n"
        f"Ø Tiefe:          {np.mean(depths):.1f}%"
    )
    _send(token, chat, msg, send_telegram)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode',        type=int,   required=True)
    parser.add_argument('--capital',     type=float, default=50.0)
    parser.add_argument('--lookback',    type=str,   default='365')
    parser.add_argument('--min-trades',  type=int,   default=5)
    parser.add_argument('--simulations', type=int,   default=10000)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    lookback_days = 365
    if args.lookback != 'auto':
        try:
            lookback_days = int(args.lookback)
        except ValueError:
            pass

    send_tg = not args.no_telegram
    token, chat = None, None
    if send_tg:
        try:
            secrets = _load_secrets()
            token, chat = _tg(secrets)
        except Exception:
            send_tg = False

    capital = args.capital
    m = args.mode

    fn_map = {
        1: lambda: analyse_walkforward_lookback(capital, args.min_trades, send_tg, token, chat),
        2: lambda: analyse_param_walkforward(capital, send_tg, token, chat),
        3: lambda: analyse_fee_impact(capital, lookback_days, send_tg, token, chat),
        4: lambda: analyse_monte_carlo(capital, lookback_days, args.simulations, send_tg, token, chat),
        5: lambda: analyse_correlation(capital, lookback_days, send_tg, token, chat),
        6: lambda: analyse_kelly(capital, lookback_days, send_tg, token, chat),
        7: lambda: analyse_regime(capital, lookback_days, send_tg, token, chat),
        8: lambda: analyse_time_of_day(capital, lookback_days, send_tg, token, chat),
        9: lambda: analyse_drawdown_duration(capital, lookback_days, send_tg, token, chat),
    }

    if m in fn_map:
        fn_map[m]()
    else:
        print(f"Unbekannter Modus: {m}")
        sys.exit(1)


if __name__ == '__main__':
    main()
