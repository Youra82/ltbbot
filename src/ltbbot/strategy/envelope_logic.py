# src/ltbbot/strategy/envelope_logic.py
import pandas as pd
import ta
import logging

logger = logging.getLogger(__name__)

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
    return df_copy, band_prices
