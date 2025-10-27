# src/ltbbot/utils/trade_manager.py (Auszug aus place_entry_orders)

def place_entry_orders(exchange, band_prices, params, balance, tracker_file_path, logger):
    """Platziert die gestaffelten Entry-, TP- und SL-Orders basierend auf Risiko."""
    # ... (Parameter holen wie gehabt) ...
    risk_per_entry_pct = risk_params.get('risk_per_entry_pct', 0.5) # Risiko pro Layer aus Config

    # --- Long Orders ---
    if behavior_params.get('use_longs', True):
        side = 'buy'
        for i, entry_limit_price in enumerate(band_prices['long']):
            # ... (Preisprüfung) ...
            try:
                # AKTUELLE LIVE-LOGIK: Risiko basiert auf AKTUELLEM Kontostand ('balance')
                # Das führt zu Compounding im Live-Handel (Risiko wächst mit dem Konto)
                risk_amount_usd = balance * (risk_per_entry_pct / 100.0)
                # ----------------------------------------------------------------------

                # OPTIONALE KONSERVATIVE ALTERNATIVE:
                # Risiko basierend auf einem festen Startkapital (müsste in Config stehen)
                # ODER einem fixen USD-Betrag pro Trade.
                # Beispiel: Annahme, Startkapital steht in Config
                # initial_capital_live = params.get('initial_capital_live', 1000) # Lese aus Config, fallback 1000
                # risk_amount_usd = initial_capital_live * (risk_per_entry_pct / 100.0)
                # logger.debug(f"Live risk based on initial capital: {risk_amount_usd:.2f} USD")
                # ----------------------------------------------------------------------

                if risk_amount_usd <= 0:
                     logger.warning(f"Risk amount <= 0 ({risk_amount_usd:.2f}) für Layer {i+1}. Skipping.")
                     continue

                # ... (Rest der Berechnung: SL-Preis, SL-Distanz, amount_coins) ...
                amount_coins = risk_amount_usd / sl_distance_price

                # ... (Mindestmengenprüfung) ...

                # ... (Order platzieren: Trigger Limit, TP Market, SL Market) ...

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Long-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders.")
                break
            except Exception as e:
                logger.error(f"Fehler beim Platzieren der Long-Order-Gruppe {i+1}: {e}", exc_info=True)

    # --- Short Orders ---
    if behavior_params.get('use_shorts', True):
        side = 'sell'
        for i, entry_limit_price in enumerate(band_prices['short']):
             # ... (Preisprüfung) ...
            try:
                # AKTUELLE LIVE-LOGIK: Risiko basiert auf AKTUELLEM Kontostand ('balance')
                risk_amount_usd = balance * (risk_per_entry_pct / 100.0)
                # ----------------------------------------------------------------------

                # OPTIONALE KONSERVATIVE ALTERNATIVE (wie oben):
                # initial_capital_live = params.get('initial_capital_live', 1000)
                # risk_amount_usd = initial_capital_live * (risk_per_entry_pct / 100.0)
                # ----------------------------------------------------------------------

                if risk_amount_usd <= 0: continue

                # ... (Rest der Berechnung: SL-Preis, SL-Distanz, amount_coins) ...
                amount_coins = risk_amount_usd / sl_distance_price

                # ... (Mindestmengenprüfung) ...

                # ... (Order platzieren: Trigger Limit, TP Market, SL Market) ...

            except ccxt.InsufficientFunds as e:
                logger.error(f"Nicht genügend Guthaben für Short-Order-Gruppe {i+1}: {e}. Stoppe weitere Orders.")
                break
            except Exception as e:
                logger.error(f"Fehler beim Platzieren der Short-Order-Gruppe {i+1}: {e}", exc_info=True)

    # ... (Tracker mit SL IDs aktualisieren) ...
