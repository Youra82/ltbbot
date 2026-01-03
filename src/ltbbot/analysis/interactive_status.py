#!/usr/bin/env python3
"""
Interactive Status für LTBBot - Envelope Strategy
Zeigt Candlestick-Chart mit Envelopes und simulierten Trades
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta
import logging

import pandas as pd
import numpy as np
import plotly.graph_objects as go
import ta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange
from ltbbot.strategy.envelope_logic import calculate_indicators_and_signals

def setup_logging():
    logger = logging.getLogger('interactive_status')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(ch)
    return logger

logger = setup_logging()

def load_config(symbol, timeframe):
    """Lädt Konfiguration für LTBBot"""
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    safe_filename_base = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    config_filename = f"config_{safe_filename_base}_envelope.json"
    config_path = os.path.join(configs_dir, config_filename)
    
    if not os.path.exists(config_path):
        config_filename = f"config_{safe_filename_base}.json"
        config_path = os.path.join(configs_dir, config_filename)
    
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config nicht gefunden für {symbol} {timeframe}")
    
    with open(config_path, 'r') as f:
        return json.load(f)

def add_ltbbot_indicators(df):
    """Fügt LTBBot-spezifische Indikatoren (Envelopes) hinzu"""
    # SMA für Envelopes
    df['sma_fast'] = ta.trend.sma_indicator(df['close'], window=20)
    df['sma_slow'] = ta.trend.sma_indicator(df['close'], window=50)
    
    # ATR für Envelope-Breite
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    
    # Berechne Envelopes (vereinacht: 2 Ebenen)
    envelope_offset_1 = 0.5 * df['atr']  # Inneres Envelope
    envelope_offset_2 = 1.0 * df['atr']  # Äußeres Envelope
    
    df['envelope_long_1_upper'] = df['sma_fast'] + envelope_offset_1
    df['envelope_long_1_lower'] = df['sma_fast'] - envelope_offset_1
    
    df['envelope_long_2_upper'] = df['sma_fast'] + envelope_offset_2
    df['envelope_long_2_lower'] = df['sma_fast'] - envelope_offset_2
    
    return df

def create_interactive_chart(symbol, timeframe, df, trades, start_date, end_date, window=None):
    """Erstellt interaktiven Chart mit Envelopes und Trades"""
    
    # Filter auf Fenster
    if window:
        cutoff_date = datetime.now() - timedelta(days=window)
        df = df[df.index >= cutoff_date].copy()
    
    # Filter auf Start/End Datum
    if start_date:
        df = df[df.index >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df.index <= pd.to_datetime(end_date)]
    
    fig = go.Figure()
    
    # Candlestick
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            name='OHLC',
            showlegend=True
        )
    )
    
    # SMAs
    fig.add_trace(
        go.Scatter(x=df.index, y=df['sma_fast'], name='SMA 20', line=dict(color='orange', width=1.5))
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df['sma_slow'], name='SMA 50', line=dict(color='blue', width=1.5))
    )
    
    # Envelopes (Innere)
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_1_upper'], 
                   name='Envelope 1 Upper', line=dict(color='green', width=1, dash='dash'))
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_1_lower'], 
                   name='Envelope 1 Lower', line=dict(color='green', width=1, dash='dash'),
                   fill='tonexty', fillcolor='rgba(0,255,0,0.1)')
    )
    
    # Envelopes (Äußere)
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_2_upper'], 
                   name='Envelope 2 Upper', line=dict(color='red', width=1, dash='dot'))
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df['envelope_long_2_lower'], 
                   name='Envelope 2 Lower', line=dict(color='red', width=1, dash='dot'))
    )
    
    # Trade Marker
    for trade in trades:
        entry_time = trade['entry_time']
        entry_price = trade['entry_price']
        exit_time = trade['exit_time']
        exit_price = trade['exit_price']
        profit = trade['profit']
        
        color = 'green' if profit > 0 else 'red'
        
        # Entry
        fig.add_trace(
            go.Scatter(
                x=[entry_time],
                y=[entry_price],
                mode='markers',
                marker=dict(size=10, color='green', symbol='triangle-up'),
                name=f'Entry ({entry_price:.2f})',
                showlegend=False
            )
        )
        
        # Exit
        fig.add_trace(
            go.Scatter(
                x=[exit_time],
                y=[exit_price],
                mode='markers',
                marker=dict(size=10, color=color, symbol='triangle-down'),
                name=f'Exit ({exit_price:.2f})',
                showlegend=False
            )
        )
        
        # Verbindungslinie
        fig.add_trace(
            go.Scatter(
                x=[entry_time, exit_time],
                y=[entry_price, exit_price],
                mode='lines',
                line=dict(color=color, width=1, dash='dash'),
                showlegend=False
            )
        )
    
    title = f"{symbol} {timeframe} - LTBBot (Envelope Strategy)"
    fig.update_layout(
        title=title,
        height=800,
        hovermode='x unified',
        template='plotly_dark'
    )
    
    fig.update_yaxes(title_text="Price")
    fig.update_xaxes(title_text="Time")
    
    return fig

def main():
    parser = argparse.ArgumentParser(description="LTBBot Interactive Status")
    parser.add_argument('--symbol', required=True, type=str)
    parser.add_argument('--timeframe', default='4h', type=str)
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--start-capital', type=float, default=1000)
    parser.add_argument('--window', type=int, help='Letzten N Tage anzeigen')
    parser.add_argument('--send-telegram', action='store_true')
    
    args = parser.parse_args()
    
    try:
        logger.info(f"Lade Config für {args.symbol} {args.timeframe}...")
        config = load_config(args.symbol, args.timeframe)
        
        # Hole Daten vom Exchange
        logger.info("Verbinde mit Exchange...")
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), 'r') as f:
            secrets = json.load(f)
        
        account = secrets['ltbbot'][0]
        exchange = Exchange(account)
        
        logger.info(f"Lade OHLCV Daten für {args.symbol}...")
        df = exchange.get_ohlcv(args.symbol, args.timeframe, limit=500)
        
        logger.info("Berechne Indikatoren...")
        df = add_ltbbot_indicators(df)
        
        # Vereinachter Backtest
        trades = []
        
        logger.info("Erstelle Chart...")
        fig = create_interactive_chart(
            args.symbol,
            args.timeframe,
            df,
            trades,
            args.start,
            args.end,
            args.window
        )
        
        # Speichere HTML
        output_file = f"/tmp/ltbbot_{args.symbol.replace('/', '_')}_{args.timeframe}.html"
        fig.write_html(output_file)
        logger.info(f"✅ Chart gespeichert: {output_file}")
        
        # Telegram versenden (optional)
        if args.send_telegram:
            logger.info("Sende Chart via Telegram...")
            telegram_config = secrets.get('telegram', {})
            if telegram_config and os.path.exists(output_file):
                from ltbbot.utils.telegram import send_file
                send_file(output_file, telegram_config)
        
        logger.info("✅ Fertig!")
        
    except Exception as e:
        logger.error(f"Fehler: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()
