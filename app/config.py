import os
from dotenv import load_dotenv
from google.generativeai.types import HarmCategory, HarmBlockThreshold

app_dir = os.path.abspath(os.path.dirname(__file__))
basedir = os.path.dirname(app_dir)

dotenv_path = os.path.join(basedir, '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
    print(f"Caricate variabili d'ambiente da: {dotenv_path}") # Log conferma
else:
    print(f"Attenzione: File .env non trovato in {basedir}")


class BaseConfig:
    """Configurazione di base da cui le altre ereditano."""

    # --- Segreti e Chiavi API (letti direttamente da environ) ---
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY')
    GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
    # Leggiamo gli SCOPES e li splittiamo subito in lista
    GOOGLE_SCOPES = os.environ.get('GOOGLE_SCOPES', "https://www.googleapis.com/auth/youtube.readonly").split()

    # --- Percorsi Assoluti (calcolati leggendo da environ) ---
    # Usiamo basedir calcolato sopra
    BASE_DIR = basedir

    # Leggi il nome/path relativo da .env e costruisci il percorso assoluto
    # Fornisci un default sensato se la variabile manca in .env
    DATABASE_FILE = os.path.join(BASE_DIR, os.environ.get('DATABASE_FILE', 'data/youtube_videos.db'))
    CHROMA_PERSIST_PATH = os.path.join(BASE_DIR, os.environ.get('CHROMA_DB_PATH', 'data/chroma_db'))
    CLIENT_SECRETS_PATH = os.path.join(BASE_DIR, os.environ.get('GOOGLE_CLIENT_SECRETS_FILE', 'client_secrets.json'))
    TOKEN_PATH = os.path.join(BASE_DIR, os.environ.get('GOOGLE_TOKEN_FILE', 'token.json'))
    UPLOAD_FOLDER_PATH = os.path.join(BASE_DIR, os.environ.get('UPLOAD_FOLDER', 'data/uploaded_docs'))
    ARTICLES_FOLDER_PATH = os.path.join(BASE_DIR, os.environ.get('ARTICLES_FOLDER', 'data/article_content'))

    # --- Impostazioni Generali App ---
    INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY')
    APP_MODE = os.environ.get('APP_MODE', 'single').lower()
    # Validazione APP_MODE
    if APP_MODE not in ['single', 'saas']:
        raise ValueError(f"Valore APP_MODE non valido nel file .env: '{APP_MODE}'. Usare 'single' o 'saas'.")
    VIDEO_COLLECTION_NAME = "video_transcripts" # Nome collezione ChromaDB
    DOCUMENT_COLLECTION_NAME = "document_content" # Potremmo usarlo per Chroma in futuro
    ARTICLE_COLLECTION_NAME = "article_content"
    ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx'} 


    # --- Impostazioni Embedding ---
    GEMINI_EMBEDDING_MODEL = "models/text-embedding-004"
    DEFAULT_CHUNK_SIZE_WORDS = 300
    DEFAULT_CHUNK_OVERLAP_WORDS = 50

    # --- Impostazioni Ricerca RAG ---
    RAG_DEFAULT_N_RESULTS = 10 # o 15, 5 troppo poco
    RAG_GENERATIVE_MODEL = "gemini-2.5-pro-exp-03-25" 
    # "gemini-1.5-flash-latest" il più veloce
    # "gemini-1.5-pro" più stitico rispetto a "gemini-2.5-pro-exp-03-25" ma meglio dei flash
    # "gemini-2.0-flash" ha gli stessi limiti di 1.5-flash-latest: risposte stitiche, non fa sommari
    # "gemini-2.5-pro-exp-03-25" ha i risultati migliori
    RAG_GENERATION_CONFIG = {
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 40,
        # "max_output_tokens": 2000, # Puoi aggiungerlo se vuoi
    }
    RAG_SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        #HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    }
    RAG_REFERENCE_DISTANCE_THRESHOLD = 0.8

    SCHEDULER_INTERVAL_UNIT = os.environ.get('SCHEDULER_INTERVAL_UNIT', 'days').lower()
    SCHEDULER_INTERVAL_VALUE_STR = os.environ.get('SCHEDULER_INTERVAL_VALUE', '1')
    # Validazione UNIT
    _valid_units = ['days', 'hours', 'minutes']
    if SCHEDULER_INTERVAL_UNIT not in _valid_units:
        print(f"ATTENZIONE: SCHEDULER_INTERVAL_UNIT ('{SCHEDULER_INTERVAL_UNIT}') non valido. Uso 'days'. Validi: {_valid_units}")
        SCHEDULER_INTERVAL_UNIT = 'days'
    # Validazione VALUE
    try:
        SCHEDULER_INTERVAL_VALUE = int(SCHEDULER_INTERVAL_VALUE_STR)
        if SCHEDULER_INTERVAL_VALUE <= 0:
            raise ValueError("Il valore deve essere positivo.")
    except (ValueError, TypeError):
        print(f"ATTENZIONE: SCHEDULER_INTERVAL_VALUE ('{SCHEDULER_INTERVAL_VALUE_STR}') non valido. Uso '1'.")
        SCHEDULER_INTERVAL_VALUE = 1


class DevelopmentConfig(BaseConfig):
    """Configurazione per lo sviluppo."""
    DEBUG = True
    # Esempio di override del percorso DB per sviluppo (se necessario)
    # DATABASE_FILE = os.path.join(BaseConfig.BASE_DIR, 'data/dev_youtube_videos.db')


class ProductionConfig(BaseConfig):
    """Configurazione per la produzione."""
    DEBUG = False


# Dizionario per selezionare la configurazione
config_by_name = dict(
    development=DevelopmentConfig,
    production=ProductionConfig,
    default=DevelopmentConfig
)

# Funzione helper per ottenere la chiave segreta (invariata)
def get_secret_key():
    key = os.environ.get('FLASK_SECRET_KEY')
    if not key:
        raise ValueError("No FLASK_SECRET_KEY set for Flask application. Set it in .env")
    return key