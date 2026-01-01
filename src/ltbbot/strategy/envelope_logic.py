# src/ltbbot/strategy/envelope_logic.py
import pandas as pd
import ta
import logging

logger = logging.getLogger(__name__)

def detect_market_regime(df, avg_period=14):
    """
    Erkennt das aktuelle Marktregime (TREND vs RANGE) mit Supertrend-Filter.
    
    Returns:
        tuple: (regime_name: str, trade_allowed: bool, trend_direction: str, supertrend_direction: str)
    """
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

        # Trend-Richtung bestimmen (für asymmetrisches Trading)
        sma_fast = ta.trend.sma_indicator(df['close'], window=20)
        sma_slow = ta.trend.sma_indicator(df['close'], window=50)

        if not sma_fast.empty and not sma_slow.empty:
            fast_val = sma_fast.iloc[-1]
            slow_val = sma_slow.iloc[-1]

            if fast_val > slow_val * 1.02:  # 2% über = klarer Uptrend
                trend_direction = "UPTREND"
            elif fast_val < slow_val * 0.98:  # 2% unter = klarer Downtrend
                trend_direction = "DOWNTREND"
            else:
                trend_direction = "NEUTRAL"
        else:
            trend_direction = "NEUTRAL"

        # Regime-Entscheidung mit detailliertem Grund
        if current_adx > 30:  # Sehr starker Trend
            logger.warning(f"STRONG_TREND: ADX={current_adx:.2f} > 30.0. Supertrend={supertrend_direction}. Trading gesperrt.")
            return "STRONG_TREND", False, trend_direction, supertrend_direction
        elif current_adx > 25:  # Starker Trend
            logger.info(f"TREND: ADX={current_adx:.2f} > 25.0. Supertrend={supertrend_direction}. Trading nur in Trendrichtung erlaubt.")
            return "TREND", True, trend_direction, supertrend_direction
        elif current_adx < 20 and price_distance_pct < 3:
            logger.info(f"RANGE: ADX={current_adx:.2f} < 20.0, price_distance_pct={price_distance_pct:.2f} < 3.0. Supertrend={supertrend_direction}. Mean-Reversion erlaubt.")
            return "RANGE", True, "NEUTRAL", supertrend_direction
        else:
            logger.info(f"UNCERTAIN: ADX={current_adx:.2f}, price_distance_pct={price_distance_pct:.2f}. Supertrend={supertrend_direction}. Vorsichtiges Trading erlaubt.")
            return "UNCERTAIN", True, trend_direction, supertrend_direction

    except Exception as e:
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

    # Optional: Hier könnte man noch explizite Signal-Spalten hinzufügen,
    # aber für die Live-Logik reichen die berechneten Bandpreise.
    # df_copy['long_signal_1'] = df_copy['low'] <= df_copy['band_low_1']
    # df_copy['short_signal_1'] = df_copy['high'] >= df_copy['band_high_1']
    # ... etc.

    df_copy.dropna(inplace=True)
    
    # Marktregime erkennen
    regime, trade_allowed, trend_direction, supertrend_direction = detect_market_regime(df_copy, avg_period)

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
