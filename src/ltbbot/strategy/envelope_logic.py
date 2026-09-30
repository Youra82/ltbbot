# src/ltbbot/strategy/envelope_logic.py
import pandas as pd
import numpy as np
import ta
import logging

logger = logging.getLogger(__name__)


def calculate_position_margin(amount_coins: float, entry_price: float, leverage: float) -> float:
    """Isolierte Margin einer Position, exakt wie Bitget sie anzeigt: Notional/Hebel.

    Geteilte Funktion fuer Live (trade_manager.py) UND Backtest/Portfolio-Simulator
    (backtester.py, portfolio_simulator.py) -- 2026-09-02 eingefuehrt, nachdem die
    Margin-Verfuegbarkeitspruefung zunaechst dreimal separat inline kopiert wurde.
    Live und Backtest muessen bei sowas immer dieselbe Funktion nutzen, sonst
    driften sie unbemerkt auseinander (siehe [[feedback_live_backtest_must_match]]).
    """
    if not leverage:
        return 0.0
    return (amount_coins * entry_price) / leverage


def margin_fits(used_margin: float, margin_required: float, available_capital: float) -> bool:
    """True, wenn eine zusaetzliche Position noch in die freie Margin passt.

    available_capital ist bei Live der echte, gerade abgerufene Kontostand
    (spiegelt bereits alle anderen offenen Positionen wider), bei Backtest/
    Portfolio-Simulator das aktuelle realisierte Kapital. Reicht es nicht, wuerde
    die Order live mit InsufficientFunds abgelehnt -- also NICHT oeffnen, statt
    auf Kredit zu simulieren/handeln.
    """
    return (used_margin + margin_required) <= available_capital


def classify_regime(adx_value, close_price, average_price, sma20, sma50, strategy_params=None):
    """Regime-Entscheidung aus den Indikatorwerten EINER abgeschlossenen Kerze.

    Geteilte Funktion fuer Live (detect_market_regime) UND Backtest/Portfolio-
    Simulator -- vorher dreimal inline kopiert, dabei driftete portfolio_simulator.py
    ab (kannte die Regime-Gate-Overrides nicht).

    Returns: (regime, trade_allowed, trend_direction)
    """
    strategy_params = strategy_params or {}
    disable_strong_trend_block = strategy_params.get('disable_strong_trend_block', False)
    strong_trend_adx_threshold = strategy_params.get('strong_trend_adx_threshold', 30.0)

    adx_value = float(adx_value) if pd.notna(adx_value) else 20.0
    if pd.notna(average_price) and average_price > 0 and pd.notna(close_price):
        price_distance_pct = abs(close_price - average_price) / average_price * 100
    else:
        price_distance_pct = 0.0

    if pd.notna(sma20) and pd.notna(sma50) and sma50 > 0 and sma20 > sma50 * 1.02:
        trend_direction = "UPTREND"
    elif pd.notna(sma20) and pd.notna(sma50) and sma50 > 0 and sma20 < sma50 * 0.98:
        trend_direction = "DOWNTREND"
    else:
        trend_direction = "NEUTRAL"

    if adx_value > strong_trend_adx_threshold:
        if disable_strong_trend_block:
            return "TREND", True, trend_direction
        return "STRONG_TREND", False, trend_direction
    if adx_value > 25:
        return "TREND", True, trend_direction
    if adx_value < 20 and price_distance_pct < 3:
        return "RANGE", True, "NEUTRAL"
    return "UNCERTAIN", True, trend_direction


def compute_band_sl_price(side, band_price, band_index, params, regime, atr_value=None):
    """Feste SL fuer ein Band (Prioritaet: sl_to_env1_ratio -> ATR-Multiplikator -> fixer %),
    im TREND/STRONG_TREND 1.5x breiter. band_index ist 0-basiert.

    Geteilte Funktion fuer Live (place_entry_orders) UND Backtest/Portfolio-Simulator.
    Gibt None zurueck, wenn kein gueltiger SL berechnet werden kann.
    """
    risk_params = params['risk']
    envelopes = params['strategy'].get('envelopes', [0.03, 0.05, 0.08])
    sl_multiplier = 1.5 if regime in ("TREND", "STRONG_TREND") else 1.0

    if 'sl_to_env1_ratio' in risk_params:
        env_pct = envelopes[band_index] if band_index < len(envelopes) else envelopes[0]
        sl_pct = env_pct * risk_params['sl_to_env1_ratio'] * sl_multiplier
    elif 'stop_loss_atr_multiplier' in risk_params:
        min_sl_pct = risk_params.get('min_stop_loss_pct', 0.5) / 100.0
        if atr_value is not None and pd.notna(atr_value) and atr_value > 0 and band_price > 0:
            sl_pct = max(float(atr_value) * risk_params['stop_loss_atr_multiplier'] / band_price, min_sl_pct)
        else:
            sl_pct = min_sl_pct
        sl_pct *= sl_multiplier
    else:
        sl_pct = risk_params['stop_loss_pct'] / 100.0 * sl_multiplier

    sl_price = band_price * (1 - sl_pct) if side == 'long' else band_price * (1 + sl_pct)
    return sl_price if sl_price > 0 else None


# Zusaetzliche Stop-Slippage im Backtest als Anteil der typischen Kerzenbewegung
# (ATR/Close der letzten abgeschlossenen Kerze). 2026-09-30: Backtester/Portfolio-Sim
# fuellten jeden Stop exakt zum SL-Preis (+ fixe 0.05%). Bei engen Stops auf
# volatilen Coins (US 4h: SL 0.13%, Kerze ~7%) rutscht eine Stop-Market-Order live
# aber ein Vielfaches davon weiter -- der Optimizer bevorzugte deshalb systematisch
# Rausch-Stops mit riesigem R:R, die live nicht halten. Mit volatilitaetsskalierter
# Slippage (BTC ~0.07%, US ~0.36%) sind solche Configs im Backtest nicht mehr
# attraktiv. Wert konservativ geschaetzt; kalibrieren, sobald echte SL-Fills gemessen.
STOP_SLIPPAGE_ATR_FRACTION = 0.05


def stop_fill_price(side, stop_price, atr_pct):
    """Realistischer Fill einer ausgeloesten Stop-Market-Order (geteilt: backtester.py,
    portfolio_simulator.py). atr_pct als Dezimal (0.03 = 3%)."""
    if atr_pct is None or atr_pct != atr_pct or atr_pct <= 0:
        return stop_price
    extra = STOP_SLIPPAGE_ATR_FRACTION * atr_pct
    return stop_price * (1 - extra) if side == 'long' else stop_price * (1 + extra)


def median_atr_pct(df, period=14):
    """Typische Kerzenbewegung: Median von ATR/Close ueber den Datensatz (Dezimal, 0.03 = 3%)."""
    if df is None or len(df) <= period:
        return float('nan')
    atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=period)
    return float((atr / df['close']).median())


def sl_atr_fraction(params, atr_pct):
    """Stop-Abstand von Band 1 als Anteil der typischen Kerzenbewegung (median ATR%).

    2026-09-30: Optimizer fand Configs mit Stops von 2-5% einer normalen Kerze (z.B.
    US 4h: SL 0.13% bei 7.2% ATR). Im Backtest wird jeder Stop exakt zum SL-Preis
    gefuellt; live rutscht eine Stop-Market-Order in solchen Bewegungen leicht ein
    Vielfaches des SL-Abstands weiter -> solche Configs sind live nicht belastbar.
    Nur fuer den SL-Modus sl_to_env1_ratio (einziger Optimizer-Modus), sonst None.
    """
    ratio = params.get('risk', {}).get('sl_to_env1_ratio')
    envelopes = params.get('strategy', {}).get('envelopes') or []
    if ratio is None or not envelopes or not atr_pct or atr_pct != atr_pct:
        return None
    return sorted(envelopes)[0] * ratio / atr_pct


def band_structure_ok(params, atr_pct, min_env1_atr, min_gap_atr):
    """Bandabstaende relativ zur typischen Kerzenbewegung (median ATR%, Dezimal).

    2026-09-30: Der Optimizer suchte Baender in festen Prozent (Band 1 ab 0.5%,
    Abstand ab 0.5%). Auf volatilen Coins lagen dadurch alle drei Baender praktisch
    auf dem MA und aufeinander (US 4h: 0.5/1.06/1.81% bei ~5-7% Kerzenbewegung) --
    die drei Einstiege loesen dann fast gleichzeitig aus (= dreifaches Risiko an
    einem Punkt statt gestaffelter Position), und Band 1 liegt im Rauschen.
    Regel: Band 1 >= min_env1_atr * ATR vom MA, jede weitere Luecke >= min_gap_atr * ATR.
    Geteilt von optimizer.py (Suchraum + Neubewertung) und sync_confirmed_flags.py.
    """
    env = sorted(params.get('strategy', {}).get('envelopes') or [])
    if not env or not atr_pct or atr_pct != atr_pct:
        return True
    if env[0] < min_env1_atr * atr_pct:
        return False
    return all((b - a) >= min_gap_atr * atr_pct for a, b in zip(env, env[1:]))


def entry_blocked_by_sl(side, current_price, sl_price):
    """Live ueberspringt ein Band, wenn der aktuelle Preis schon jenseits dessen SL liegt
    (Entry wuerde sofort gestoppt). Geteilt mit Backtest/Portfolio-Simulator."""
    if current_price is None or sl_price is None:
        return False
    return current_price < sl_price if side == 'long' else current_price > sl_price


def simulate_entry_fill(side, candle_open, candle_high, candle_low, candle_close, trigger_price, sl_price):
    """Fill einer Live-Entry-Order (Bitget-Trigger-Limit) innerhalb EINER Coarse-Kerze,
    deren Baender aus der vorherigen, abgeschlossenen Kerze stammen (wie live).

    Live-Mechanik (siehe live_sim.py, an echten Fills 2026-08/09 geprueft):
    - Liegt der Preis bei Platzierung (~Kerzen-Open) schon jenseits des SL, wird
      das Band uebersprungen (entry_blocked_by_sl). Live gilt das nur fuer den
      jeweiligen 15-Min-Zyklus (kommt der Preis zurueck, wird spaeter noch
      platziert) -- auf Coarse-Kerzen nicht exakt abbildbar. Bewusst die
      KONSERVATIVE Wahl: Abgleich gegen live_sim.py (1m, Sept. 2026, 5 Configs)
      ergab Skip = 38 Trades/WR 18.4%/-5.35 USDT, Nicht-Skip = 89/24.7%/+5.48,
      live_sim = 64/18.7%/-8.30. Skip unterschaetzt die Trade-Anzahl, trifft
      aber Winrate und PnL -- fuer Optimizer/OOS-Gate sind Scheinfunde teurer.
    - Die Ausloese-Richtung der Plan-Order ergibt sich aus Trigger vs. Preis bei
      Platzierung. Liegt der Preis schon jenseits des Triggers (typisch nach
      Close-Confirmation), feuert sie erst, wenn der Preis ZURUECK zum Trigger
      laeuft. Fill ~ Triggerpreis (Limit am Band ist dann marketable).
    - SL-Treffer in der Entry-Kerze:
      * Order wartet auf Bewegung Richtung SL (Long: Preis faellt zum Trigger):
        wird das SL-Niveau in der Kerze erreicht, lief der Preis zwingend erst
        durch den Trigger -> Stop.
      * Order wartet auf Ruecklauf (Long: Preis steigt zum Trigger): ein SL-Kontakt
        VOR dem Fill loest keinen Stop aus (reduceOnly-SL ohne Position). Die
        Reihenfolge ist auf Coarse-Kerzen unbekannt -- als Stop gewertet, wenn
        die Kerze jenseits des SL schliesst (gleiche Regel wie live_sim.py je 1m-Bar).

    Returns: (fill_price, stopped_in_entry_candle) oder (None, False) ohne Fill.
    """
    if entry_blocked_by_sl(side, candle_open, sl_price):
        return None, False
    if side == 'long':
        if candle_open > trigger_price:
            if candle_low > trigger_price:
                return None, False
            return trigger_price, bool(candle_low <= sl_price)
        if candle_high < trigger_price:
            return None, False
        return trigger_price, bool(candle_close <= sl_price)
    else:
        if candle_open < trigger_price:
            if candle_high < trigger_price:
                return None, False
            return trigger_price, bool(candle_high >= sl_price)
        if candle_low > trigger_price:
            return None, False
        return trigger_price, bool(candle_close >= sl_price)


def detect_market_regime(df, avg_period=14, silent=False, strategy_params=None):
    """
    Erkennt das aktuelle Marktregime (TREND vs RANGE) mit Supertrend-Filter.

    Args:
        df: DataFrame mit OHLC-Daten
        avg_period: Periode für Durchschnittsberechnung
        silent: Wenn True, keine Log-Ausgaben (für Backtest)
        strategy_params: optionales dict mit Overrides für das ADX-Regime-Gate
            (disable_strong_trend_block, strong_trend_adx_threshold) -- muss
            IDENTISCH zu den Overrides im Backtester (run_envelope_backtest)
            interpretiert werden, damit Live und Backtest konsistent bleiben.

    Returns:
        tuple: (regime_name: str, trade_allowed: bool, trend_direction: str, supertrend_direction: str)
    """
    strategy_params = strategy_params or {}
    disable_strong_trend_block = strategy_params.get('disable_strong_trend_block', False)
    strong_trend_adx_threshold = strategy_params.get('strong_trend_adx_threshold', 30.0)
    try:
        # ADX für Trendstärke berechnen
        adx = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
        current_adx = adx.iloc[-1] if not adx.empty else 20

        # Preis-Position zum gleitenden Durchschnitt
        if 'average' in df.columns and not df['average'].empty:
            current_price = df['close'].iloc[-1]
            ma = df['average'].iloc[-1]
            price_distance_pct = abs(current_price - ma) / ma * 100 if ma > 0 else 0
        else:
            price_distance_pct = 0

        # Supertrend-Indikator (übergeordneter Trendfilter)
        # Periode 10, Multiplier 3 für mittelfristigen Trend
        supertrend_indicator = ta.trend.STCIndicator(
            close=df['close'],
            window_slow=50,
            window_fast=23,
            cycle=10,
            smooth1=3,
            smooth2=3
        )
        
        # Alternativ: Einfacher Supertrend basierend auf ATR
        try:
            atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=10)
            hl2 = (df['high'] + df['low']) / 2
            multiplier = 3.0
            
            upperband = hl2 + (multiplier * atr)
            lowerband = hl2 - (multiplier * atr)
            
            # Supertrend Richtung
            supertrend_direction = "NEUTRAL"
            if not upperband.empty and not lowerband.empty:
                if current_price > upperband.iloc[-1]:
                    supertrend_direction = "BULLISH"
                elif current_price < lowerband.iloc[-1]:
                    supertrend_direction = "BEARISH"
                else:
                    supertrend_direction = "NEUTRAL"
        except Exception as e:
            logger.debug(f"Supertrend-Berechnung fehlgeschlagen: {e}")
            supertrend_direction = "NEUTRAL"

        # Trend-Richtung + Regime-Entscheidung: geteilte Funktion mit Backtest/Portfolio-Sim
        sma_fast = ta.trend.sma_indicator(df['close'], window=20)
        sma_slow = ta.trend.sma_indicator(df['close'], window=50)
        fast_val = sma_fast.iloc[-1] if not sma_fast.empty else float('nan')
        slow_val = sma_slow.iloc[-1] if not sma_slow.empty else float('nan')
        close_val = df['close'].iloc[-1]
        avg_val = df['average'].iloc[-1] if 'average' in df.columns and not df['average'].empty else float('nan')

        regime, trade_allowed, trend_direction = classify_regime(
            current_adx, close_val, avg_val, fast_val, slow_val, strategy_params)

        if not silent:
            if regime == "STRONG_TREND":
                logger.warning(f"STRONG_TREND: ADX={current_adx:.2f} > {strong_trend_adx_threshold:.1f}. Supertrend={supertrend_direction}. Trading gesperrt.")
            elif regime == "TREND" and current_adx > strong_trend_adx_threshold:
                logger.info(f"TREND (Gate deaktiviert): ADX={current_adx:.2f} > {strong_trend_adx_threshold:.1f}. "
                            f"Supertrend={supertrend_direction}. Trading in Trendrichtung erlaubt (Block deaktiviert).")
            elif regime == "TREND":
                logger.info(f"TREND: ADX={current_adx:.2f} > 25.0. Supertrend={supertrend_direction}. Trading nur in Trendrichtung erlaubt.")
            elif regime == "RANGE":
                logger.info(f"RANGE: ADX={current_adx:.2f} < 20.0, price_distance_pct={price_distance_pct:.2f} < 3.0. Supertrend={supertrend_direction}. Mean-Reversion erlaubt.")
            else:
                logger.info(f"UNCERTAIN: ADX={current_adx:.2f}, price_distance_pct={price_distance_pct:.2f}. Supertrend={supertrend_direction}. Vorsichtiges Trading erlaubt.")
        return regime, trade_allowed, trend_direction, supertrend_direction

    except Exception as e:
        if not silent:
            logger.warning(f"Fehler bei Marktregime-Erkennung: {e}. Defaulte auf UNCERTAIN.")
        return "UNCERTAIN", True, "NEUTRAL", "NEUTRAL"

def calculate_indicators_and_signals(df, params):
    """
    Berechnet die Envelope-Indikatoren und identifiziert potenzielle Ein- und Ausstiegspunkte.

    Returns:
        pd.DataFrame: DataFrame mit Indikatoren und potenziellen Signalen.
        dict: Dictionary mit den berechneten Preisen für die letzte Kerze.
    """
    strategy_params = params['strategy']
    avg_type = strategy_params['average_type']
    avg_period = strategy_params['average_period']
    envelopes = strategy_params['envelopes']

    df_copy = df.copy()

    # --- Berechne den zentralen Durchschnitt ---
    if avg_type == 'DCM':
        ta_obj = ta.volatility.DonchianChannel(df_copy['high'], df_copy['low'], df_copy['close'], window=avg_period)
        df_copy['average'] = ta_obj.donchian_channel_mband()
    elif avg_type == 'SMA':
        df_copy['average'] = ta.trend.sma_indicator(df_copy['close'], window=avg_period)
    elif avg_type == 'EMA':
        df_copy['average'] = ta.trend.ema_indicator(df_copy['close'], window=avg_period)
    elif avg_type == 'WMA':
        df_copy['average'] = ta.trend.wma_indicator(df_copy['close'], window=avg_period)
    else:
        raise ValueError(f"Ungültiger average_type: {avg_type}")

    # --- Berechne ATR für SL-Berechnung ---
    atr_period = params.get('risk', {}).get('stop_loss_atr_period', 14)
    df_copy['atr'] = ta.volatility.average_true_range(
        df_copy['high'], df_copy['low'], df_copy['close'], window=atr_period
    )

    # --- Berechne die Envelopes ---
    band_prices = {'average': None, 'long': [], 'short': []}
    for i, e_pct in enumerate(envelopes):
        band_num = i + 1
        high_col = f'band_high_{band_num}'
        low_col = f'band_low_{band_num}'
        df_copy[high_col] = df_copy['average'] / (1 - e_pct)
        df_copy[low_col] = df_copy['average'] * (1 - e_pct)

        # Speichere die letzten Bandpreise für die Orderplatzierung
        if not df_copy.empty:
             last_low_price = df_copy[low_col].iloc[-1]
             last_high_price = df_copy[high_col].iloc[-1]
             band_prices['long'].append(last_low_price)
             band_prices['short'].append(last_high_price)

    if not df_copy.empty:
        band_prices['average'] = df_copy['average'].iloc[-1]
        band_prices['atr'] = float(df_copy['atr'].iloc[-1]) if pd.notna(df_copy['atr'].iloc[-1]) else None

    # Optional: Hier könnte man noch explizite Signal-Spalten hinzufügen,
    # aber für die Live-Logik reichen die berechneten Bandpreise.
    # df_copy['long_signal_1'] = df_copy['low'] <= df_copy['band_low_1']
    # df_copy['short_signal_1'] = df_copy['high'] >= df_copy['band_high_1']
    # ... etc.

    df_copy.dropna(inplace=True)
    
    # Marktregime erkennen (Regime-Gate-Overrides aus strategy_params, falls gesetzt --
    # muss konsistent mit den Overrides im Backtester (run_envelope_backtest) sein)
    regime, trade_allowed, trend_direction, supertrend_direction = detect_market_regime(
        df_copy, avg_period, strategy_params=strategy_params)

    # ADX und price_distance_pct für Logging extrahieren
    try:
        adx = ta.trend.adx(df_copy['high'], df_copy['low'], df_copy['close'], window=14)
        band_prices['adx'] = float(adx.iloc[-1]) if not adx.empty else None
    except Exception:
        band_prices['adx'] = None
    try:
        if 'average' in df_copy.columns and not df_copy['average'].empty:
            current_price = df_copy['close'].iloc[-1]
            ma = df_copy['average'].iloc[-1]
            band_prices['price_distance_pct'] = float(abs(current_price - ma) / ma * 100) if ma > 0 else None
        else:
            band_prices['price_distance_pct'] = None
    except Exception:
        band_prices['price_distance_pct'] = None

    # Erweitere band_prices um Regime-Info
    band_prices['regime'] = regime
    band_prices['trade_allowed'] = trade_allowed
    band_prices['trend_direction'] = trend_direction
    band_prices['supertrend_direction'] = supertrend_direction

    return df_copy, band_prices
