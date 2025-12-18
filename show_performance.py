# Performance Report Generator für LTBBOT
# Zeigt detaillierte Performance-Statistiken aller aktiven Strategien

import sys
import os
import json

# Pfad Setup
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from ltbbot.utils.performance_monitor import generate_performance_report
from ltbbot.utils.trade_manager import get_tracker_file_path

def main():
    print("=" * 80)
    print("LTBBOT v2.0 - PERFORMANCE REPORT")
    print("=" * 80)
    print()
    
    # Lade settings.json um aktive Strategien zu finden
    settings_file = os.path.join(PROJECT_ROOT, 'settings.json')
    
    try:
        with open(settings_file, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        print(f"❌ Fehler beim Laden von settings.json: {e}")
        return
    
    strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    
    if not strategies:
        print("⚠️  Keine Strategien in settings.json gefunden.")
        return
    
    print(f"Gefundene Strategien: {len(strategies)}")
    print()
    
    # Zeige Report für jede Strategie
    for strategy in strategies:
        symbol = strategy.get('symbol')
        timeframe = strategy.get('timeframe')
        is_active = strategy.get('active', False)
        
        if not symbol or not timeframe:
            continue
        
        status = "🟢 AKTIV" if is_active else "🔴 INAKTIV"
        print(f"{status} - {symbol} ({timeframe})")
        print("-" * 80)
        
        # Generiere Report
        tracker_path = get_tracker_file_path(symbol, timeframe)
        
        if os.path.exists(tracker_path):
            try:
                # Lade Tracker-Daten
                with open(tracker_path, 'r') as f:
                    tracker_data = json.load(f)
                
                # Zeige Performance-Daten
                perf = tracker_data.get('performance', {})
                
                if perf and perf.get('total_trades', 0) > 0:
                    total = perf.get('total_trades', 0)
                    wins = perf.get('winning_trades', 0)
                    losses = perf.get('losing_trades', 0)
                    win_rate = perf.get('win_rate', 0)
                    consec_losses = perf.get('consecutive_losses', 0)
                    max_consec = perf.get('max_consecutive_losses', 0)
                    
                    print(f"   📊 Total Trades: {total}")
                    print(f"   ✅ Gewinner: {wins}")
                    print(f"   ❌ Verlierer: {losses}")
                    print(f"   📈 Win-Rate: {win_rate:.1f}%")
                    print(f"   🔄 Aktuelle Verlust-Serie: {consec_losses}")
                    print(f"   ⚠️  Max. Verlust-Serie: {max_consec}")
                    
                    # Bewertung
                    if win_rate >= 45:
                        print(f"   💚 AUSGEZEICHNET")
                    elif win_rate >= 40:
                        print(f"   💚 GUT")
                    elif win_rate >= 35:
                        print(f"   💛 BEFRIEDIGEND")
                    elif win_rate >= 30:
                        print(f"   🧡 SCHWACH")
                    elif win_rate >= 25:
                        print(f"   ❤️  KRITISCH")
                    else:
                        print(f"   🚨 INAKZEPTABEL")
                    
                    # Tracker-Status
                    status = tracker_data.get('status', 'unknown')
                    last_side = tracker_data.get('last_side')
                    
                    print(f"   🔧 Tracker-Status: {status}")
                    if last_side:
                        print(f"   📌 Letzte SL-Seite: {last_side}")
                else:
                    print(f"   ℹ️  Keine Trade-Daten vorhanden")
                
            except Exception as e:
                print(f"   ❌ Fehler beim Lesen der Tracker-Datei: {e}")
        else:
            print(f"   ℹ️  Keine Tracker-Datei vorhanden (noch keine Trades)")
        
        print()
    
    print("=" * 80)
    print()
    
    # Zusammenfassung
    active_count = sum(1 for s in strategies if s.get('active', False))
    print(f"📊 Zusammenfassung:")
    print(f"   Total Strategien: {len(strategies)}")
    print(f"   Aktive Strategien: {active_count}")
    print(f"   Inaktive Strategien: {len(strategies) - active_count}")
    print()
    
    # Empfehlungen
    print("💡 Empfehlungen:")
    print("   - Win-Rate > 40%: ✅ Strategie läuft gut")
    print("   - Win-Rate 30-40%: ⚠️  Beobachten")
    print("   - Win-Rate < 30%: 🚨 Anpassungen erforderlich")
    print("   - 5+ Verluste in Folge: 🛡️ Risiko-Reduktion aktiv")
    print()
    print("=" * 80)

if __name__ == "__main__":
    main()
