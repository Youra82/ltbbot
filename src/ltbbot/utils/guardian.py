# src/ltbbot/utils/guardian.py
import logging
from functools import wraps
# *** Importpfad an ltbbot angepasst ***
from ltbbot.utils.telegram import send_message

def guardian_decorator(func):
    """
    Ein Decorator, der eine Funktion umschließt, um alle unerwarteten
    Ausnahmen abzufangen, sie zu protokollieren und eine Telegram-Warnung zu senden,
    anstatt den Prozess abstürzen zu lassen.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Die Logger- und Telegram-Konfiguration sind normalerweise in den args oder kwargs
        logger = None
        telegram_config = {}
        params = {}

        # Finde die relevanten Objekte in den Argumenten
        for arg in args:
            if isinstance(arg, logging.Logger):
                logger = arg
            if isinstance(arg, dict) and 'bot_token' in arg:
                telegram_config = arg
            if isinstance(arg, dict) and 'market' in arg:
                params = arg

        if not logger:
            # Fallback, falls kein Logger übergeben wird
            logger = logging.getLogger("guardian_fallback")
            logger.setLevel(logging.ERROR)
            if not logger.handlers:
                logger.addHandler(logging.StreamHandler())

        try:
            # Führe die eigentliche Bot-Funktion aus (z.B. run_for_account)
            return func(*args, **kwargs)
        
        except Exception as e:
            # Wenn ein Fehler auftritt, fange ihn ab
            symbol = params.get('market', {}).get('symbol', 'Unbekannt')
            timeframe = params.get('market', {}).get('timeframe', 'N/A')

            error_message = f"Ein kritischer Systemfehler ist im Guardian-Decorator für {symbol} ({timeframe}) aufgetreten."
            
            logger.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            logger.critical("!!! KRITISCHER SYSTEMFEHLER IM GUARDIAN !!!")
            logger.critical(f"!!! Strategie: {symbol} ({timeframe})")
            logger.critical(f"!!! Fehler: {e}", exc_info=True) # Loggt den vollen Traceback
            logger.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

            # Sende eine Telegram-Nachricht
            try:
                # *** Bot-Name an ltbbot angepasst ***
                telegram_message = f"🚨 *Kritischer Systemfehler* im ltbbot Guardian für *{symbol} ({timeframe})*:\n\n`{e.__class__.__name__}: {e}`\n\nDer Prozess wird neu gestartet."
                send_message(
                    telegram_config.get('bot_token'),
                    telegram_config.get('chat_id'),
                    telegram_message
                )
            except Exception as tel_e:
                logger.error(f"Konnte keine Telegram-Fehlermeldung senden: {tel_e}")
            
            # Wichtig: Wirf den Fehler weiter, damit der master_runner
            # den Exit-Code sieht und den Prozess neu starten kann.
            raise e 

    return wrapper
