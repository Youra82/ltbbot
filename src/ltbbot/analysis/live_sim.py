# src/ltbbot/analysis/live_sim.py
"""Ehrlicher Live-Mechanik-Simulator fuer die Envelope-Strategie (2026-09-26).

Warum: backtester.py/portfolio_simulator.py nutzten Baender, TP-MA und Regime der
LAUFENDEN Kerze (inkl. deren Close = Lookahead) und fuellten Entries sofort zum
Bandpreis. Live (trade_manager.full_trade_cycle) arbeitet anders -- dieser
Simulator bildet den echten Ablauf auf 1m-Kerzen nach:

- Alle 15 Min ein Zyklus (Cron, +1 Min Verzoegerung): Baender/TP/Regime kommen
  aus der letzten ABGESCHLOSSENEN Kerze (drop_incomplete_last_candle).
- Pro Zyklus: offene Entry-Orders stornieren und neu platzieren; Band wird
  uebersprungen, wenn der Live-Preis schon jenseits des SL liegt.
- Entry = Bitget-Plan-Order (Trigger-Limit): die Ausloese-RICHTUNG ergibt sich
  aus Trigger vs. Preis bei Platzierung. Liegt der Preis schon jenseits des
  Triggers (typisch nach Close-Confirmation), feuert die Order erst, wenn der
  Preis ZURUECK zum Trigger laeuft. Fill ~ Triggerpreis (echte Fills 2026-08/09
  bestaetigt: NEAR -0.068% vs. Trigger -0.067%, XRP +0.185% vs. +0.179%).
- SL pro Band fest, wird MIT der Entry-Order platziert. Wird das SL-Niveau vor
  dem Fill beruehrt, verpufft die reduceOnly-SL (keine Position) -- ein spaeterer
  Fill ist dann ungedeckt und wird beim naechsten Zyklus von
  check_naked_position() zwangsgeschlossen.
- TP gebuendelt an der MA der letzten abgeschlossenen Kerze, erst ab dem
  naechsten Zyklus nach dem Fill aktiv (manage_existing_position()).
- Same-Candle-Sperre: ein geschlossenes Band wird in derselben Kerze nicht neu
  eroeffnet (arm_same_candle_reentry_guard()).
- Risiko/Margin auf freiem Kapital (wie fetch_balance_usdt()), Min-Notional 5 USDT.

Indikatoren kommen aus derselben Funktion wie live
(envelope_logic.calculate_indicators_and_signals).
"""
import numpy as np
import pandas as pd
import ta
from numba import njit

from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

FEE = 0.0006          # Taker (Trigger-Limit ist bei Ausloesung marketable, Exits = Market)
SLIP_EXIT = 0.0005    # Market-Exit (SL/TP/Zwangsschliessung)
SLIP_ENTRY = 0.0002   # Fill vs. Trigger (live beobachtet ~0.001-0.01%)
MIN_NOTIONAL = 5.0
CYCLE_MIN = 15
CYCLE_OFFSET_MIN = 1


def prepare_coarse(coarse: pd.DataFrame, params: dict):
    """Arrays je Coarse-Kerze c, die WAEHREND Kerze c gelten (= Werte von c-1)."""
    sp = params['strategy']
    df, _ = calculate_indicators_and_signals(coarse.copy(), params)
    # calculate_indicators_and_signals droppt NaN-Zeilen -- auf volle Kerzenliste zurueck
    df = df.reindex(coarse.index)
    n_env = len(sp['envelopes'])

    adx = ta.trend.adx(coarse['high'], coarse['low'], coarse['close'], window=14)
    sma20 = ta.trend.sma_indicator(coarse['close'], window=20)
    sma50 = ta.trend.sma_indicator(coarse['close'], window=50)
    thr = sp.get('strong_trend_adx_threshold', 30.0)
    disable_block = sp.get('disable_strong_trend_block', False)
    close = coarse['close']
    avg = df['average']
    dist = (close - avg).abs() / avg * 100

    up = sma20 > sma50 * 1.02
    down = sma20 < sma50 * 0.98
    adx_f = adx.fillna(20.0)
    strong = adx_f > thr
    trend = (adx_f > 25) & ~strong
    rng = (adx_f < 20) & (dist < 3)
    allow = ~(strong & (not disable_block))
    is_trend = trend | strong
    # RANGE -> Richtung NEUTRAL (keine Seitensperre)
    useL = ~(down & ~(rng & ~is_trend))
    useS = ~(up & ~(rng & ~is_trend))
    slmult = np.where(is_trend, 1.5, 1.0)
    warm = np.arange(len(coarse)) < 49

    def sh(a, fill):
        a = np.asarray(a, dtype=np.float64)
        out = np.empty_like(a); out[0] = fill; out[1:] = a[:-1]
        return out

    bandL = np.full((len(coarse), n_env), np.nan)
    bandS = np.full((len(coarse), n_env), np.nan)
    confL = np.zeros((len(coarse), n_env), dtype=np.bool_)
    confS = np.zeros((len(coarse), n_env), dtype=np.bool_)
    for k in range(n_env):
        bl = df[f'band_low_{k+1}'].to_numpy(np.float64)
        bh = df[f'band_high_{k+1}'].to_numpy(np.float64)
        cl = close.to_numpy(np.float64)
        bandL[1:, k] = bl[:-1]; bandS[1:, k] = bh[:-1]
        confL[1:, k] = cl[:-1] <= bl[:-1]
        confS[1:, k] = cl[:-1] >= bh[:-1]
    confL &= ~np.isnan(bandL); confS &= ~np.isnan(bandS)

    allow_a = sh(allow.to_numpy(np.float64), 1.0) > 0.5
    useL_a = sh(useL.to_numpy(np.float64), 1.0) > 0.5
    useS_a = sh(useS.to_numpy(np.float64), 1.0) > 0.5
    # Warmup wie Live/Backtester: < 49 Kerzen -> UNCERTAIN/NEUTRAL, erlaubt
    wsh = sh(warm.astype(np.float64), 1.0) > 0.5
    allow_a[wsh] = True; useL_a[wsh] = True; useS_a[wsh] = True
    slm = sh(np.where(wsh, 1.0, slmult) if False else slmult, 1.0)
    slm[wsh] = 1.0
    tp = sh(avg.to_numpy(np.float64), np.nan)
    return dict(bandL=bandL, bandS=bandS, confL=confL, confS=confS, allow=allow_a,
                useL=useL_a, useS=useS_a, slmult=slm, tp=tp)


def prepare_fine(fine: pd.DataFrame, coarse_index: pd.DatetimeIndex):
    ts = fine.index
    cidx = coarse_index.searchsorted(ts, side='right') - 1
    minutes = (ts.hour * 60 + ts.minute).to_numpy()
    is_cycle = (minutes % CYCLE_MIN) == CYCLE_OFFSET_MIN
    return dict(o=fine['open'].to_numpy(np.float64), h=fine['high'].to_numpy(np.float64),
                l=fine['low'].to_numpy(np.float64), c=fine['close'].to_numpy(np.float64),
                cidx=cidx.astype(np.int64), is_cycle=is_cycle, t=ts.asi8 // 60_000_000_000)


@njit(cache=True)
def _kernel(o, h, l, cl, cidx, is_cycle, tmin, j_start,
            bandL, bandS, confL, confS, allow, useL, useS, slmult, tp_arr,
            env, sl_ratio, trig_delta, risk_pct, leverage, use_longs, use_shorts,
            start_capital):
    n = o.shape[0]
    K = env.shape[0]
    # Zustand je Seite (0=long, 1=short) und Band
    p_open = np.zeros((2, K), np.bool_)
    p_entry = np.zeros((2, K)); p_amt = np.zeros((2, K)); p_sl = np.zeros((2, K))
    p_marg = np.zeros((2, K)); p_naked = np.zeros((2, K), np.bool_)
    p_t = np.zeros((2, K), np.int64)
    a_on = np.zeros((2, K), np.bool_); a_T = np.zeros((2, K)); a_B = np.zeros((2, K))
    a_up = np.zeros((2, K), np.bool_); a_sl = np.zeros((2, K)); a_amt = np.zeros((2, K))
    a_marg = np.zeros((2, K)); a_slgone = np.zeros((2, K), np.bool_)
    guard = np.full((2, K), -1, np.int64)
    tp_on = np.zeros(2, np.bool_); tp_lvl = np.zeros(2); tp_up = np.zeros(2, np.bool_)

    capital = start_capital
    used = 0.0
    maxT = 20000
    tr = np.zeros((maxT, 8))  # entry_t, exit_t, side, band, entry, exit, pnl, reason
    nt = 0
    eq_peak = start_capital; max_dd = 0.0

    for j in range(j_start, n):
        c = cidx[j]
        if c < 1:
            continue
        # ---------------- Zyklus am Bar-Open ----------------
        if is_cycle[j]:
            px = o[j]
            # Notfall: ungedeckte Position (SL verpufft) -> Zwangsschliessung
            for s in range(2):
                for k in range(K):
                    if p_open[s, k] and p_naked[s, k]:
                        ex = px * (1 - SLIP_EXIT) if s == 0 else px * (1 + SLIP_EXIT)
                        e = p_entry[s, k]; a = p_amt[s, k]
                        pnl = ((ex - e) if s == 0 else (e - ex)) * a - (e * a + ex * a) * FEE
                        capital += pnl; used -= p_marg[s, k]; p_open[s, k] = False
                        guard[s, k] = c
                        if nt < maxT:
                            tr[nt, 0] = p_t[s, k]; tr[nt, 1] = tmin[j]; tr[nt, 2] = s; tr[nt, 3] = k + 1
                            tr[nt, 4] = e; tr[nt, 5] = ex; tr[nt, 6] = pnl; tr[nt, 7] = 3; nt += 1
            # alle Entry-Orders stornieren
            for s in range(2):
                for k in range(K):
                    a_on[s, k] = False
            # TP gebuendelt aktualisieren
            tpv = tp_arr[c]
            for s in range(2):
                has = False
                for k in range(K):
                    if p_open[s, k]:
                        has = True
                if has and tpv == tpv and tpv > 0:
                    tp_on[s] = True; tp_lvl[s] = tpv; tp_up[s] = px < tpv
                else:
                    tp_on[s] = False
            # neue Entry-Orders
            if allow[c] and capital > 0:
                side_open = -1
                for s in range(2):
                    for k in range(K):
                        if p_open[s, k]:
                            side_open = s
                free = capital - used
                cyc_used = 0.0
                for s in range(2):
                    if side_open >= 0 and side_open != s:
                        continue
                    if s == 0 and not (use_longs and useL[c]):
                        continue
                    if s == 1 and not (use_shorts and useS[c]):
                        continue
                    risk_usd = free * risk_pct / 100.0
                    if risk_usd <= 0:
                        continue
                    for k in range(K):
                        if p_open[s, k] or guard[s, k] == c:
                            continue
                        if s == 0:
                            B = bandL[c, k]; ok = confL[c, k]
                        else:
                            B = bandS[c, k]; ok = confS[c, k]
                        if not ok or not (B > 0):
                            continue
                        slp = env[k] * sl_ratio * slmult[c]
                        if s == 0:
                            sl = B * (1 - slp)
                            if px < sl:
                                continue
                            T = B * (1 - trig_delta)
                        else:
                            sl = B * (1 + slp)
                            if px > sl:
                                continue
                            T = B * (1 + trig_delta)
                        dist = abs(B - sl)
                        if dist <= 0:
                            continue
                        amt = risk_usd / dist
                        if amt * B < MIN_NOTIONAL:
                            continue
                        marg = amt * B / leverage
                        if cyc_used + marg > free:
                            continue
                        cyc_used += marg
                        a_on[s, k] = True; a_T[s, k] = T; a_B[s, k] = B; a_sl[s, k] = sl
                        a_amt[s, k] = amt; a_marg[s, k] = marg; a_slgone[s, k] = False
                        a_up[s, k] = px < T

        # ---------------- Intrabar: offene Positionen ----------------
        for s in range(2):
            tp_hit = False; tp_px = 0.0
            if tp_on[s]:
                lv = tp_lvl[s]
                if tp_up[s] and h[j] >= lv:
                    tp_hit = True; tp_px = max(lv, o[j])
                elif (not tp_up[s]) and l[j] <= lv:
                    tp_hit = True; tp_px = min(lv, o[j])
            any_closed = False
            for k in range(K):
                if not p_open[s, k]:
                    continue
                ex = 0.0; reason = 0
                if not p_naked[s, k]:
                    sl = p_sl[s, k]
                    if s == 0 and l[j] <= sl:
                        ex = min(sl, o[j]) * (1 - SLIP_EXIT); reason = 1
                    elif s == 1 and h[j] >= sl:
                        ex = max(sl, o[j]) * (1 + SLIP_EXIT); reason = 1
                if reason == 0 and tp_hit:
                    ex = tp_px * (1 - SLIP_EXIT) if s == 0 else tp_px * (1 + SLIP_EXIT); reason = 2
                if reason > 0:
                    e = p_entry[s, k]; a = p_amt[s, k]
                    pnl = ((ex - e) if s == 0 else (e - ex)) * a - (e * a + ex * a) * FEE
                    capital += pnl; used -= p_marg[s, k]; p_open[s, k] = False
                    guard[s, k] = c; any_closed = True
                    if nt < maxT:
                        tr[nt, 0] = p_t[s, k]; tr[nt, 1] = tmin[j]; tr[nt, 2] = s; tr[nt, 3] = k + 1
                        tr[nt, 4] = e; tr[nt, 5] = ex; tr[nt, 6] = pnl; tr[nt, 7] = reason; nt += 1
            if tp_hit and any_closed:
                still = False
                for k in range(K):
                    if p_open[s, k]:
                        still = True
                if not still:
                    tp_on[s] = False
        if used < 0:
            used = 0.0

        # ---------------- Intrabar: Entry-Orders ----------------
        for s in range(2):
            for k in range(K):
                if not a_on[s, k]:
                    continue
                sl = a_sl[s, k]
                # SL-Niveau vor Fill beruehrt -> reduceOnly-SL verpufft
                if (s == 0 and l[j] <= sl) or (s == 1 and h[j] >= sl):
                    a_slgone[s, k] = True
                T = a_T[s, k]; B = a_B[s, k]
                fill = -1.0
                if s == 0:
                    if a_up[s, k]:
                        if h[j] >= T:
                            if o[j] < T:
                                fill = T
                            elif o[j] <= B:
                                fill = o[j]
                            elif l[j] <= B:
                                fill = B
                    else:
                        if l[j] <= T:
                            fill = min(T, o[j])
                else:
                    if not a_up[s, k]:
                        if l[j] <= T:
                            if o[j] > T:
                                fill = T
                            elif o[j] >= B:
                                fill = o[j]
                            elif h[j] >= B:
                                fill = B
                    else:
                        if h[j] >= T:
                            fill = max(T, o[j])
                if fill <= 0:
                    continue
                fill = fill * (1 + SLIP_ENTRY) if s == 0 else fill * (1 - SLIP_ENTRY)
                a_on[s, k] = False
                p_open[s, k] = True; p_entry[s, k] = fill; p_amt[s, k] = a_amt[s, k]
                p_sl[s, k] = sl; p_marg[s, k] = a_marg[s, k]; p_t[s, k] = tmin[j]
                used += a_marg[s, k]
                # SL war schon vor dem Fill ausgeloest (verpufft) -> ungedeckt
                wait_down_long = (s == 0 and not a_up[s, k])
                wait_up_short = (s == 1 and a_up[s, k])
                slgone_before = a_slgone[s, k]
                if wait_down_long or wait_up_short:
                    # Bewegung Richtung SL geht durch den Trigger: SL im selben Bar = echter SL-Hit
                    slgone_before = False
                p_naked[s, k] = slgone_before
                if not p_naked[s, k]:
                    same_bar_sl = False
                    if wait_down_long and l[j] <= sl:
                        same_bar_sl = True
                    elif wait_up_short and h[j] >= sl:
                        same_bar_sl = True
                    elif s == 0 and a_up[s, k] and cl[j] <= sl:
                        same_bar_sl = True
                    elif s == 1 and (not a_up[s, k]) and cl[j] >= sl:
                        same_bar_sl = True
                    if same_bar_sl:
                        ex = sl * (1 - SLIP_EXIT) if s == 0 else sl * (1 + SLIP_EXIT)
                        e = fill; a = p_amt[s, k]
                        pnl = ((ex - e) if s == 0 else (e - ex)) * a - (e * a + ex * a) * FEE
                        capital += pnl; used -= p_marg[s, k]; p_open[s, k] = False
                        guard[s, k] = c
                        if nt < maxT:
                            tr[nt, 0] = tmin[j]; tr[nt, 1] = tmin[j]; tr[nt, 2] = s; tr[nt, 3] = k + 1
                            tr[nt, 4] = e; tr[nt, 5] = ex; tr[nt, 6] = pnl; tr[nt, 7] = 1; nt += 1

        # Equity/Drawdown (realisiert + unrealisiert zum Close)
        eq = capital
        for s in range(2):
            for k in range(K):
                if p_open[s, k]:
                    if s == 0:
                        eq += (cl[j] - p_entry[s, k]) * p_amt[s, k]
                    else:
                        eq += (p_entry[s, k] - cl[j]) * p_amt[s, k]
        if eq > eq_peak:
            eq_peak = eq
        if eq_peak > 0:
            dd = (eq_peak - eq) / eq_peak
            if dd > max_dd:
                max_dd = dd
        if capital <= 0:
            break

    # offene Positionen am Ende zum letzten Close bewerten
    final = capital
    for s in range(2):
        for k in range(K):
            if p_open[s, k]:
                if s == 0:
                    final += (cl[n - 1] - p_entry[s, k]) * p_amt[s, k]
                else:
                    final += (p_entry[s, k] - cl[n - 1]) * p_amt[s, k]
    return final, max_dd, tr[:nt]


REASONS = {1: 'SL', 2: 'TP', 3: 'NAKED_CLOSE'}


def simulate(coarse: pd.DataFrame, fine: pd.DataFrame, params: dict, start_capital: float = 50.0,
             sim_start=None, sim_end=None, prepared=None):
    """coarse: volle OHLCV-Historie des Strategie-Timeframes (inkl. Warmup),
    fine: 1m-OHLCV. sim_start/sim_end begrenzen den gehandelten Zeitraum.
    Gibt dict mit pnl_pct, trades, win_rate, max_dd_pct, profit_factor, trades_df."""
    rp, sp, bp = params['risk'], params['strategy'], params.get('behavior', {})
    if 'sl_to_env1_ratio' not in rp:
        raise ValueError('live_sim unterstuetzt nur SL-Modus sl_to_env1_ratio (einziger Optimizer-Modus).')
    if prepared is None:
        prepared = prepare_coarse(coarse, params)
    f = fine
    if sim_end is not None:
        f = f[f.index < pd.Timestamp(sim_end, tz='UTC') if pd.Timestamp(sim_end).tzinfo is None else f.index < pd.Timestamp(sim_end)]
    fa = prepare_fine(f, coarse.index)
    j_start = 0
    if sim_start is not None:
        st = pd.Timestamp(sim_start)
        st = st.tz_localize('UTC') if st.tzinfo is None else st
        j_start = int(f.index.searchsorted(st))
    final, mdd, tr = _kernel(fa['o'], fa['h'], fa['l'], fa['c'], fa['cidx'], fa['is_cycle'], fa['t'], j_start,
                             prepared['bandL'], prepared['bandS'], prepared['confL'], prepared['confS'],
                             prepared['allow'], prepared['useL'], prepared['useS'], prepared['slmult'],
                             prepared['tp'], np.asarray(sp['envelopes'], np.float64), float(rp['sl_to_env1_ratio']),
                             float(sp.get('trigger_price_delta_pct', 0.05)) / 100.0,
                             float(rp.get('risk_per_entry_pct', 0.5)), float(rp['leverage']),
                             bool(bp.get('use_longs', True)), bool(bp.get('use_shorts', True)),
                             float(start_capital))
    tdf = pd.DataFrame(tr, columns=['entry_t', 'exit_t', 'side', 'band', 'entry', 'exit', 'pnl', 'reason'])
    if not tdf.empty:
        tdf['entry_t'] = pd.to_datetime(tdf['entry_t'].astype(np.int64), unit='m', utc=True)
        tdf['exit_t'] = pd.to_datetime(tdf['exit_t'].astype(np.int64), unit='m', utc=True)
        tdf['side'] = np.where(tdf['side'] == 0, 'long', 'short')
        tdf['band'] = tdf['band'].astype(int)
        tdf['reason'] = tdf['reason'].astype(int).map(REASONS)
    wins = tdf['pnl'][tdf['pnl'] > 0].sum() if not tdf.empty else 0.0
    losses = -tdf['pnl'][tdf['pnl'] < 0].sum() if not tdf.empty else 0.0
    return {
        'pnl_pct': (final - start_capital) / start_capital * 100,
        'end_capital': final,
        'trades': len(tdf),
        'win_rate': float((tdf['pnl'] > 0).mean() * 100) if len(tdf) else 0.0,
        'max_dd_pct': mdd * 100,
        'profit_factor': (wins / losses) if losses > 0 else (np.inf if wins > 0 else 0.0),
        'trades_df': tdf,
    }
