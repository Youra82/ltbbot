#!/usr/bin/env python3
"""
Analyse-Skript zur Überprüfung der Equity Curve Berechnung
"""

import os
import sys
import json
import pandas as pd
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.exchange import Exchange
from ltbbot.analysis.backtester import run_envelope_backtest, load_data, calculate_indicators_and_signals

def main():
    # Lade eine Test-Konfiguration
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'ltbbot', 'strategy', 'configs')
    config_files = [f for f in os.listdir(configs_dir) if f.startswith('config_') and f.endswith('.json')]
    
    if not config_files:
        print("❌ Keine Konfigurationsdateien gefunden!")
        return
    
    # Nutze die erste Konfiguration
    config_path = os.path.join(configs_dir, config_files[0])
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    symbol = config.get('symbol', 'BTC/USDT')
    timeframe = config.get('timeframe', '1h')
    start_capital = config.get('start_capital', 1000)
    
    print(f"\n{'='*80}")
    print(f"Analyse der Equity Curve")
    print(f"{'='*80}")
    print(f"Konfiguration: {config_files[0]}")
    print(f"Symbol: {symbol}")
    print(f"Timeframe: {timeframe}")
    print(f"Start Capital: {start_capital} USDT")
    print(f"{'='*80}\n")
    
    # Lade Daten
    print(f"📊 Lade Daten für {symbol} ({timeframe})...")
    try:
        data = load_data(symbol, timeframe, '2025-01-01', '2025-01-03')
        if data.empty:
            print("❌ Keine Daten geladen!")
            return
        print(f"✅ {len(data)} Kerzen geladen\n")
    except Exception as e:
        print(f"❌ Fehler beim Laden: {e}")
        return
    
    # Führe Backtest durch
    print(f"⚙️  Starte Backtest...")
    try:
        backtest_result = run_envelope_backtest(data, config['strategy'], start_capital=start_capital)
        print(f"✅ Backtest abgeschlossen\n")
    except Exception as e:
        print(f"❌ Fehler beim Backtest: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Analysiere Ergebnisse
    print(f"📈 BACKTEST ERGEBNISSE:")
    print(f"{'─'*80}")
    print(f"  Start Capital:      {backtest_result.get('start_capital', 'N/A')} USDT")
    print(f"  End Capital:        {backtest_result.get('end_capital', 'N/A')} USDT")
    print(f"  Total PnL:          {backtest_result.get('total_pnl_pct', 'N/A'):.2f}%")
    print(f"  Trades:             {backtest_result.get('trades_count', 0)}")
    print(f"  Win Rate:           {backtest_result.get('win_rate', 0):.1f}%")
    print(f"  Max Drawdown:       {backtest_result.get('max_drawdown_pct', 0):.2f}%")
    print(f"{'─'*80}\n")
    
    # Analysiere Equity Curve
    equity_curve = backtest_result.get('equity_curve', [])
    if not equity_curve:
        print("❌ Keine Equity Curve Daten!")
        return
    
    equity_df = pd.DataFrame(equity_curve)
    equity_df['timestamp'] = pd.to_datetime(equity_df['timestamp'])
    equity_df = equity_df.sort_values('timestamp').reset_index(drop=True)
    
    print(f"📊 EQUITY CURVE ANALYSE:")
    print(f"{'─'*80}")
    print(f"  Anzahl Datenpunkte: {len(equity_df)}")
    print(f"  Min Wert:           {equity_df['equity'].min():.2f} USDT")
    print(f"  Max Wert:           {equity_df['equity'].max():.2f} USDT")
    print(f"  Erstes Datum:       {equity_df.iloc[0]['timestamp']}")
    print(f"  Letztes Datum:      {equity_df.iloc[-1]['timestamp']}")
    print(f"{'─'*80}\n")
    
    # Zeige erste und letzte Einträge
    print(f"📋 ERSTE 10 EQUITY-PUNKTE:")
    print(f"{'─'*80}")
    print(f"{'Index':<6} {'Timestamp':<25} {'Equity (USDT)':<15} {'Change':<15}")
    print(f"{'─'*80}")
    for i in range(min(10, len(equity_df))):
        row = equity_df.iloc[i]
        timestamp = row['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        equity = row['equity']
        if i == 0:
            change = f"START: {start_capital:.2f}"
        else:
            prev_equity = equity_df.iloc[i-1]['equity']
            change = f"{equity - prev_equity:+.2f} ({(equity/prev_equity - 1)*100:+.2f}%)"
        print(f"{i:<6} {timestamp:<25} {equity:>14.2f} {change:>14}")
    print(f"{'─'*80}\n")
    
    # Zeige letzte 10 Einträge
    print(f"📋 LETZTE 10 EQUITY-PUNKTE:")
    print(f"{'─'*80}")
    print(f"{'Index':<6} {'Timestamp':<25} {'Equity (USDT)':<15} {'Change':<15}")
    print(f"{'─'*80}")
    start_idx = max(0, len(equity_df) - 10)
    for i in range(start_idx, len(equity_df)):
        row = equity_df.iloc[i]
        timestamp = row['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        equity = row['equity']
        prev_equity = equity_df.iloc[i-1]['equity']
        change = f"{equity - prev_equity:+.2f} ({(equity/prev_equity - 1)*100:+.2f}%)" if i > 0 else "INIT"
        print(f"{i:<6} {timestamp:<25} {equity:>14.2f} {change:>14}")
    print(f"{'─'*80}\n")
    
    # Verifiziere Konsistenz
    print(f"🔍 KONSISTENZ-CHECK:")
    print(f"{'─'*80}")
    
    # Check 1: Start Capital
    first_equity = equity_df.iloc[0]['equity']
    if abs(first_equity - start_capital) < 0.01:
        print(f"  ✅ Erste Equity = Start Capital: {first_equity:.2f}")
    else:
        print(f"  ⚠️  WARNUNG: Erste Equity ({first_equity:.2f}) ≠ Start Capital ({start_capital:.2f})")
    
    # Check 2: End Capital
    last_equity = equity_df.iloc[-1]['equity']
    reported_end_capital = backtest_result.get('end_capital', 0)
    if abs(last_equity - reported_end_capital) < 0.01:
        print(f"  ✅ Letzte Equity = Reported End Capital: {last_equity:.2f}")
    else:
        print(f"  ⚠️  WARNUNG: Letzte Equity ({last_equity:.2f}) ≠ Reported End Capital ({reported_end_capital:.2f})")
    
    # Check 3: PnL Berechnung
    calculated_pnl_pct = ((last_equity - start_capital) / start_capital) * 100
    reported_pnl_pct = backtest_result.get('total_pnl_pct', 0)
    if abs(calculated_pnl_pct - reported_pnl_pct) < 0.01:
        print(f"  ✅ Berechnete PnL = Reported PnL: {reported_pnl_pct:.2f}%")
    else:
        print(f"  ⚠️  WARNUNG: Berechnete PnL ({calculated_pnl_pct:.2f}%) ≠ Reported PnL ({reported_pnl_pct:.2f}%)")
    
    # Check 4: Nur bei Trade Exits sollten sich Werte ändern
    unchanged_count = 0
    changes = 0
    for i in range(1, len(equity_df)):
        if abs(equity_df.iloc[i]['equity'] - equity_df.iloc[i-1]['equity']) < 0.01:
            unchanged_count += 1
        else:
            changes += 1
    
    print(f"  ℹ️  Datenpunkte ohne Änderung: {unchanged_count}")
    print(f"  ℹ️  Datenpunkte mit Änderung (Trades): {changes}")
    
    print(f"{'─'*80}\n")
    
    print(f"✅ Analyse abgeschlossen!")

if __name__ == '__main__':
    main()
