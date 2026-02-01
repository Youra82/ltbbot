# src/ltbbot/utils/telegram.py
import requests
import logging
import os # Added for file path check

logger = logging.getLogger(__name__)

def send_message(bot_token, chat_id, message):
    """Sends a plain text message to a Telegram chat."""
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token or Chat-ID not configured. Message not sent.")
        return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id, 
        'text': message, 
        'parse_mode': 'HTML'
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': escaped_message, 'parse_mode': 'MarkdownV2'}

    try:
        response = requests.post(api_url, data=payload, timeout=10) # 10 second timeout
        response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
        logger.debug(f"Telegram message sent successfully. Response: {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending Telegram message: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending Telegram message: {e}")

def send_document(bot_token, chat_id, file_path, caption=""):
    """Sends a document (e.g., a CSV or PNG file) to a Telegram chat."""
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token or Chat-ID not configured. Document not sent.")
        return

    if not os.path.exists(file_path):
         logger.error(f"File not found for sending to Telegram: {file_path}")
         return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    payload = {
        'chat_id': chat_id,
        'caption': caption
    }

    try:
        with open(file_path, 'rb') as doc_file:
            files = {'document': (os.path.basename(file_path), doc_file)} # Include filename
            response = requests.post(api_url, data=payload, files=files, timeout=30) # Increased timeout for uploads
            response.raise_for_status()
            logger.debug(f"Telegram document sent successfully. Response: {response.text}")
    except FileNotFoundError:
        # Should have been caught earlier, but double-check
        logger.error(f"File not found during Telegram upload attempt: {file_path}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending document via Telegram: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending document via Telegram: {e}")

# You might also need send_photo if generate_and_send_chart.py uses it directly
def send_photo(bot_token, chat_id, photo_path, caption=""):
    """Sends a photo to a Telegram chat."""
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token or Chat-ID not configured. Photo not sent.")
        return

    if not os.path.exists(photo_path):
         logger.error(f"Photo file not found for sending to Telegram: {photo_path}")
         return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    payload = {
        'chat_id': chat_id,
        'caption': caption
    }

    try:
        with open(photo_path, 'rb') as photo_file:
            files = {'photo': (os.path.basename(photo_path), photo_file)}
            response = requests.post(api_url, data=payload, files=files, timeout=30)
            response.raise_for_status()
            logger.debug(f"Telegram photo sent successfully. Response: {response.text}")
    except FileNotFoundError:
         logger.error(f"Photo file not found during Telegram upload attempt: {photo_path}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending photo via Telegram: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending photo via Telegram: {e}")
