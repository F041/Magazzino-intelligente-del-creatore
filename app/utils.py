import secrets
import string
import sqlite3
import logging
from flask import current_app
from datetime import datetime 
import secrets
import string
import os


logger = logging.getLogger(__name__)

def generate_api_key(length=40):
    """Genera una chiave API sicura e casuale."""
    alphabet = string.ascii_letters + string.digits
    prefix = "sk_"
    random_length = length - len(prefix)
    if random_length <= 0:
        raise ValueError("La lunghezza richiesta per la chiave API è troppo corta.")
    random_part = ''.join(secrets.choice(alphabet) for _ in range(random_length))
    return prefix + random_part

def build_full_config_for_background_process(user_id: str) -> dict:
    """
    Costruisce un dizionario di configurazione completo, unendo la configurazione di base
    dell'app con le impostazioni personalizzate (e non vuote) dell'utente.
    ORA CONSERVA ANCHE I MODELLI DI DEFAULT DEL SISTEMA.
    """
    full_config = {**current_app.config}

    if 'RAG_MODELS_LIST' in full_config:
        full_config['DEFAULT_RAG_MODELS_LIST_FROM_ENV'] = full_config['RAG_MODELS_LIST'][:] 

    if 'USE_AGENTIC_CHUNKING' not in full_config:
        full_config['USE_AGENTIC_CHUNKING'] = os.environ.get('USE_AGENTIC_CHUNKING', 'False')
    
    if user_id:
        db_path = current_app.config.get('DATABASE_FILE')
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT llm_provider, llm_model_name, llm_embedding_model, llm_api_key, ollama_base_url FROM user_settings WHERE user_id = ?", (user_id,))
            settings_row = cursor.fetchone()
            
            if settings_row:
                logger.info(f"Trovate impostazioni utente per il processo (user: {user_id}). Applico sovrascritture.")
                
                user_provider = settings_row['llm_provider']
                if user_provider and user_provider.strip():
                    full_config['llm_provider'] = user_provider

                user_model_name = settings_row['llm_model_name']
                if user_model_name and user_model_name.strip():
                    full_config['llm_model_name'] = user_model_name
                    # Sovrascriviamo la lista RAG con quella dell'utente
                    full_config['RAG_MODELS_LIST'] = [m.strip() for m in user_model_name.split(',') if m.strip()]

                user_embedding_model = settings_row['llm_embedding_model']
                if user_embedding_model and user_embedding_model.strip():
                    full_config['llm_embedding_model'] = user_embedding_model

                user_api_key = settings_row['llm_api_key']
                if user_api_key and user_api_key.strip():
                    full_config['llm_api_key'] = user_api_key
                    if full_config.get('llm_provider') in ['google', 'groq']:
                        full_config['GOOGLE_API_KEY'] = user_api_key

                user_ollama_url = settings_row['ollama_base_url']
                if user_ollama_url and user_ollama_url.strip():
                    full_config['ollama_base_url'] = user_ollama_url

        except sqlite3.Error as e:
            logger.error(f"Impossibile caricare le impostazioni utente per il processo (user: {user_id}): {e}")
        finally:
            if conn:
                conn.close()
    
    return full_config


def format_datetime_filter(value, format='%d %b %Y'):
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