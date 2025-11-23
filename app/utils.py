import secrets
import string
import sqlite3
import logging
from flask import current_app
from datetime import datetime 
import secrets
import string
import os
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


logger = logging.getLogger(__name__)

# TODO: conviene aggiunger altro qui?
'''
tipo cosa?
'''

def normalize_url(raw_url: str) -> str:
    """
    Normalizza un URL per confronto/stoccaggio:
    - rimuove frammenti (#...)
    - rimuove parametri UTM comuni
    - rimuove trailing slash finale (mantiene "/" se path è root)
    - rende scheme e netloc in lower-case
    - ricompone l'URL in forma canonica
    """
    if not raw_url or not isinstance(raw_url, str):
        return raw_url

    try:
        parsed = urlparse(raw_url.strip())
        if not parsed.scheme or not parsed.netloc:
            return raw_url.strip()

        # Rimuovi fragment
        fragment = ''

        # Filtra query params indesiderati (UTM; estendi la lista se serve)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered_pairs = [(k, v) for (k, v) in query_pairs if not k.lower().startswith('utm_')]

        # Ricostruisci la query in modo deterministico
        new_query = urlencode(filtered_pairs, doseq=True)

        # Normalizza path (rimuovi trailing slash salvo root '/')
        path = parsed.path.rstrip('/')
        if path == '':
            path = '/'

        normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, '', new_query, fragment))
        return normalized
    except Exception:
        # In caso di problemi, restituisce la stringa pulita senza spazi esterni
        return raw_url.strip()




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
                    # Conserviamo la chiave generica
                    full_config['llm_api_key'] = user_api_key

                    # MAPPIAMO esplicitamente la chiave alle variabili che i vari moduli si aspettano.
                    # Questo evita che _index_article/_index_page non trovino la chiave in core_config.
                    # Se in futuro aggiungiamo altri provider, estendiamo questa mappatura.
                    full_config['GOOGLE_API_KEY'] = user_api_key
                    # Se usi nomi diversi per embedding (es. GEMINI), manteniamo anche quello se non già impostato
                    if not full_config.get('GEMINI_EMBEDDING_API_KEY'):
                        full_config['GEMINI_EMBEDDING_API_KEY'] = user_api_key


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
    
def log_system_alert(alert_type: str, message: str, details: str = None):
    """
    Registra un avviso di sistema nel DB e mantiene pulita la tabella
    conservando solo gli ultimi 50 record (Log Rotation).
    """
    try:
        db_path = current_app.config.get('DATABASE_FILE')
        if not db_path: return

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 1. Inserisci il nuovo avviso
        cursor.execute(
            "INSERT INTO system_alerts (alert_type, message, details) VALUES (?, ?, ?)",
            (alert_type, message, details)
        )

        # 2. Pulizia Automatica: Cancella tutto tranne gli ultimi 50 record
        # Questa query elimina gli ID che NON sono nei top 50 ordinati per data
        cursor.execute("""
            DELETE FROM system_alerts 
            WHERE id NOT IN (
                SELECT id FROM system_alerts 
                ORDER BY created_at DESC 
                LIMIT 50
            )
        """)
        
        conn.commit()
        # logger.info(f"System Alert registrato: {alert_type}") # Decommenta se vuoi loggarlo anche su console
    except Exception as e:
        logger.error(f"Impossibile registrare system alert: {e}")
    finally:
        if conn: conn.close()