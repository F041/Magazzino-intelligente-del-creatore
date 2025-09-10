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
    COHERE_API_KEY = os.environ.get('COHERE_API_KEY')
    # Leggiamo gli SCOPES e li splittiamo subito in lista
    GOOGLE_SCOPES = os.environ.get('GOOGLE_SCOPES', "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/youtubepartner").split()
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
    RAG_DEFAULT_N_RESULTS = 50 # o 15, 5 troppo poco
    # NUOVA LOGICA per la lista di modelli con fallback
    # Leggiamo la stringa dal .env, fornendo un default stabile se manca
    _models_str = os.environ.get('LLM_MODELS', "gemini-2.5-pro,gemini-2.0-flash")
    # Puliamo la stringa e la trasformiamo in una lista, rimuovendo eventuali modelli vuoti
    RAG_MODELS_LIST = [model.strip() for model in _models_str.split(',') if model.strip()]
    # Aggiungiamo un controllo per evitare che la lista sia vuota a causa di un errore di configurazione
    if not RAG_MODELS_LIST:
        print("ATTENZIONE: LLM_MODELS è vuota o mal formattata. Uso un modello di default.")
        RAG_MODELS_LIST = ["gemini-1.5-pro-latest"]
    print(f"Modelli RAG caricati in ordine di preferenza: {RAG_MODELS_LIST}") # Log di conferma all'avvio
    # "gemini-1.5-flash-latest" il più veloce
    # "gemini-1.5-pro" più stitico rispetto a "gemini-2.5-pro-exp-03-25" ma meglio dei flash
    # "gemini-2.0-flash" ha gli stessi limiti di 1.5-flash-latest: risposte stitiche, non fa sommari
    # "gemini-2.5-pro-exp-03-25" ha i risultati migliori -> sostituito con gemini-2.5-flash-preview-04-17
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
    SCHEDULER_RUN_HOUR_STR = os.environ.get('SCHEDULER_RUN_HOUR', '4') # Legge la nuova variabile
    # Validazione per assicurarsi che sia un numero valido (0-23)
    try:
        SCHEDULER_RUN_HOUR = int(SCHEDULER_RUN_HOUR_STR)
        if not 0 <= SCHEDULER_RUN_HOUR <= 23:
            raise ValueError("L'ora deve essere tra 0 e 23.")
    except (ValueError, TypeError):
        print(f"ATTENZIONE: SCHEDULER_RUN_HOUR ('{SCHEDULER_RUN_HOUR_STR}') non valido. Uso '4'.")
        SCHEDULER_RUN_HOUR = 4
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

class TestConfig(DevelopmentConfig):
    """Configurazione per i test."""
    TESTING = True
    SECRET_KEY = 'test_secret_key'
    GOOGLE_API_KEY = 'test_google_api_key_placeholder' # Non verranno fatte chiamate reali

    # _TEST_BASE_DIR verrà impostato dalla fixture di test
    _TEST_BASE_DIR = None
    _DATA_SUBDIR_IN_TEST_DIR = None

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == '_TEST_BASE_DIR' and value is not None:
            self._DATA_SUBDIR_IN_TEST_DIR = os.path.join(value, "data_for_tests")
            os.makedirs(self._DATA_SUBDIR_IN_TEST_DIR, exist_ok=True)

    def _get_test_data_path(self, filename):
        if self._DATA_SUBDIR_IN_TEST_DIR is None:
            raise ValueError("_DATA_SUBDIR_IN_TEST_DIR non è stato impostato. Assicurati che _TEST_BASE_DIR sia impostato.")
        return os.path.join(self._DATA_SUBDIR_IN_TEST_DIR, filename)

    @property
    def DATABASE_FILE(self):
        return self._get_test_data_path('test_magazzino.db')

    @property
    def CHROMA_PERSIST_PATH(self):
        return self._get_test_data_path('test_chroma_db')

    @property
    def TOKEN_PATH(self):
        return self._get_test_data_path('test_token.json')

    @property
    def UPLOAD_FOLDER_PATH(self):
        return self._get_test_data_path('test_uploaded_docs')

    @property
    def ARTICLES_FOLDER_PATH(self):
        return self._get_test_data_path('test_article_content')

    @property
    def CLIENT_SECRETS_PATH(self):
        if self._TEST_BASE_DIR is None:
             raise ValueError("_TEST_BASE_DIR non è stato impostato. Assicurati che la fixture di test lo imposti.")
        return os.path.join(self._TEST_BASE_DIR, 'test_client_secrets.json')


    APP_MODE = 'single'
    # Per disabilitare lo scheduler, modifica create_app in main.py
    # per non avviarlo se app.config['TESTING'] è True.


# Dizionario per selezionare la configurazione
config_by_name = dict(
    development=DevelopmentConfig,
    production=ProductionConfig,
    test=TestConfig, # Aggiungi TestConfig al dizionario
    default=DevelopmentConfig
)

# Funzione helper per ottenere la chiave segreta (invariata)
def get_secret_key():
    key = os.environ.get('FLASK_SECRET_KEY')
    if not key:
        raise ValueError("No FLASK_SECRET_KEY set for Flask application. Set it in .env")
    return key
