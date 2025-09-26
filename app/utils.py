import secrets
import string
import sqlite3
import logging
from flask import current_app
from datetime import datetime 
import secrets
import string


logger = logging.getLogger(__name__)

def generate_api_key(length=40):
    """Genera una chiave API sicura e casuale."""
    # Usa lettere maiuscole/minuscole, numeri. Escludi caratteri ambigui se vuoi.
    alphabet = string.ascii_letters + string.digits
    # Aggiungi un prefisso per riconoscibilità (opzionale)
    prefix = "sk_" # Simula "secret key"
    # Calcola lunghezza parte casuale
    random_length = length - len(prefix)
    if random_length <= 0:
        raise ValueError("La lunghezza richiesta per la chiave API è troppo corta.")
    # Genera parte casuale
    random_part = ''.join(secrets.choice(alphabet) for _ in range(random_length))
    return prefix + random_part

def build_full_config_for_background_process(user_id: str) -> dict:
    """
    Costruisce un dizionario di configurazione completo per i processi in background,
    unendo la configurazione di base dell'app con le impostazioni personalizzate
    dell'utente salvate nel database.
    """
    if not user_id:
        return {**current_app.config}

    # 1. Inizia con la configurazione di base dell'applicazione
    full_config = {**current_app.config}

    # 2. Recupera le impostazioni specifiche dell'utente dal DB
    user_settings = {}
    db_path = current_app.config.get('DATABASE_FILE')
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Selezioniamo solo le colonne che ci interessano per sovrascrivere
        cursor.execute("SELECT llm_provider, llm_model_name, llm_embedding_model, llm_api_key, ollama_base_url FROM user_settings WHERE user_id = ?", (user_id,))
        settings_row = cursor.fetchone()
        if settings_row:
            # Filtriamo via i valori None per non sovrascrivere una config valida con un valore nullo
            user_settings = {k: v for k, v in dict(settings_row).items() if v is not None}
            logger.info(f"Trovate impostazioni utente per il processo background: {list(user_settings.keys())}")
    except sqlite3.Error as e:
        logger.error(f"Impossibile caricare le impostazioni utente per il thread (user: {user_id}): {e}")
    finally:
        if conn:
            conn.close()
    
    # 3. "Fondi" le impostazioni, dando priorità a quelle dell'utente
    full_config.update(user_settings)
    
    return full_config

def format_datetime_filter(value, format='%d %b %Y'):
    """
    Filtro Jinja per formattare stringhe di data in formato ISO (con o senza 'Z').
    Restituisce la stringa formattata o il valore originale in caso di errore.
    """
    # Se il valore non è una stringa o è troppo corto per essere una data valida, lo restituiamo subito.
    if not isinstance(value, str) or len(value) <= 1:
        return value

    try:
        if value.endswith('Z'):
            dt_object = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            dt_object = datetime.fromisoformat(value)
        
        return dt_object.strftime(format)
            
    except (ValueError, TypeError) as e:
        logger.warning(f"Filtro format_date: Impossibile analizzare il valore '{value}'. Errore: {e}")
        return value