
import os
import sqlite3
import logging
from flask import current_app
from google.oauth2.credentials import Credentials
import google.auth.transport.requests

logger = logging.getLogger(__name__)

# --- Helper Functions  ---

def save_credentials(credentials):
    """Salva le credenziali nel file specificato nella config."""
    # Questa funzione ora richiede un contesto applicativo per current_app
    if not current_app:
        logger.error("save_credentials chiamata senza contesto applicativo attivo.")
        return
    token_path = current_app.config.get('TOKEN_PATH')
    if not token_path:
        logger.error("Percorso TOKEN_PATH non trovato nella configurazione dell'app.")
        return
    try:
        with open(token_path, 'w') as token:
            token.write(credentials.to_json())
        logger.info(f"Credenziali salvate in: {token_path}")
    except IOError as e:
        logger.error(f"Errore I/O salvando credenziali in {token_path}: {e}")
    except Exception as e:
        logger.exception(f"Errore inatteso salvando credenziali: {e}")


def load_credentials():
    """Carica le credenziali dal file specificato nella config."""
    # Questa funzione ora richiede un contesto applicativo per current_app
    if not current_app:
        logger.error("load_credentials chiamata senza contesto applicativo attivo.")
        return None
    token_path = current_app.config.get('TOKEN_PATH')
    scopes = current_app.config.get('GOOGLE_SCOPES')

    if not token_path or not scopes:
        logger.error(f"Configurazione mancante: TOKEN_PATH={token_path}, GOOGLE_SCOPES={scopes}")
        return None

    if not os.path.exists(token_path):
        logger.info(f"File credenziali '{token_path}' non trovato.")
        return None

    logger.info(f"Tentativo caricamento credenziali da: {token_path}")
    try:
        creds = Credentials.from_authorized_user_file(token_path, scopes)
        if creds and creds.valid:
            logger.info("Credenziali valide trovate.")
            return creds
        elif creds and creds.expired and creds.refresh_token:
            logger.info("Credenziali scadute, tentativo refresh...")
            try:
                creds.refresh(google.auth.transport.requests.Request())
                # Tentativo di salvataggio (potrebbe fallire se chiamato al di fuori di una richiesta)
                logger.warning("Tentativo salvataggio credenziali aggiornate da load_credentials.")
                save_credentials(creds) # Richiede contesto app
                logger.info("Token aggiornato con successo.")
                return creds
            except Exception as e:
                # ERRORE REFRESH: Qui deve cancellare il file e restituire None
                logger.error(f"Errore aggiornamento token: {e}. Necessaria ri-autenticazione.")
                try: os.remove(token_path); logger.info(f"File token rimosso dopo errore refresh: {token_path}") # Log aggiunto
                except OSError: pass
                return None # <-- Fondamentale restituire None qui
        else:
            # Credenziali non valide o senza refresh: Cancella e restituisci None
            logger.warning("Credenziali non valide o senza refresh token. Rimuovo file token.")
            try: os.remove(token_path); logger.info(f"File token rimosso perché invalido/senza refresh: {token_path}") # Log aggiunto
            except OSError: pass
            return None # <-- Fondamentale restituire None qui
    except Exception as e:
        # Errore generale caricamento: Cancella e restituisci None
        logger.error(f"Errore caricamento/parsing credenziali da {token_path}: {e}")
        try: os.remove(token_path); logger.info(f"File token rimosso dopo errore caricamento: {token_path}") # Log aggiunto
        except OSError: pass
        return None # <-- Fondamentale restituire None qui

    # Aggiunta finale per sicurezza: se si arriva qui inaspettatamente, restituisce None
    logger.warning("load_credentials ha raggiunto la fine senza restituire un valore esplicito. Restituisco None.")
    return None


# --- Setup Directory (usando config object) ---

def setup_chroma_directory(config): # Accetta config object
    """Assicura che la directory per ChromaDB esista."""
    chroma_path = config.get('CHROMA_PERSIST_PATH') # Usa config passata
    if not chroma_path:
         raise ValueError("CHROMA_PERSIST_PATH non trovato nella configurazione.")
    if not os.path.exists(chroma_path):
        try:
             os.makedirs(chroma_path)
             logger.info(f"Directory ChromaDB '{chroma_path}' creata.")
        except OSError as e:
             logger.error(f"Errore creando directory ChromaDB {chroma_path}: {e}")
             raise # Rilancia errore


def init_db(config):
    """Inizializza il database SQLite usando il path dalla config."""
    db_path = config.get('DATABASE_FILE')
    if not db_path:
        raise ValueError("DATABASE_FILE non trovato nella configurazione.")

    db_dir = os.path.dirname(db_path)
    if not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir)
            logger.info(f"Directory database '{db_dir}' creata.")
        except OSError as e:
            logger.error(f"Errore creando la directory {db_dir}: {e}")
            raise

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # --- Tabella videos ---
        # 1. Crea la tabella SE NON ESISTE (senza user_id inizialmente per semplicità)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
                channel_id TEXT NOT NULL, published_at TEXT NOT NULL, transcript TEXT,
                transcript_language TEXT, captions_type TEXT,
                description TEXT,
                processing_status TEXT DEFAULT 'pending', added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        # 2. Tenta di AGGIUNGERE la colonna user_id se non esiste già
        try:
            cursor.execute("ALTER TABLE videos ADD COLUMN user_id TEXT")
            logger.info("Colonna 'user_id' aggiunta alla tabella 'videos'.")
        except sqlite3.OperationalError as e:
            # Ignora l'errore se la colonna esiste già ("duplicate column name")
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'user_id' già presente nella tabella 'videos'.")
            else:
                raise # Rilancia altri errori OperationalError

        # --- Tabella documents ---
        # 1. Crea la tabella SE NON ESISTE
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY, original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL UNIQUE, filepath TEXT NOT NULL,
                filesize INTEGER, mimetype TEXT,
                processing_status TEXT DEFAULT 'pending',
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        # 2. Tenta di AGGIUNGERE la colonna user_id
        try:
            cursor.execute("ALTER TABLE documents ADD COLUMN user_id TEXT")
            logger.info("Colonna 'user_id' aggiunta alla tabella 'documents'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'user_id' già presente nella tabella 'documents'.")
            else:
                raise

        # --- Tabella articles ---
        # 1. Crea la tabella SE NON ESISTE
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS articles (
                article_id TEXT PRIMARY KEY, guid TEXT UNIQUE, feed_url TEXT,
                article_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, published_at TEXT,
                extracted_content_path TEXT, content_hash TEXT,
                processing_status TEXT DEFAULT 'pending',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        # 2. Tenta di AGGIUNGERE la colonna user_id
        try:
            cursor.execute("ALTER TABLE articles ADD COLUMN user_id TEXT")
            logger.info("Colonna 'user_id' aggiunta alla tabella 'articles'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'user_id' già presente nella tabella 'articles'.")
            else:
                raise

        # --- Tabella pages ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pages (
                page_id TEXT PRIMARY KEY,
                page_url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                published_at TEXT,
                extracted_content_path TEXT,
                content_hash TEXT,
                processing_status TEXT DEFAULT 'pending',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        try:
            cursor.execute("ALTER TABLE pages ADD COLUMN user_id TEXT")
            logger.info("Colonna 'user_id' aggiunta alla tabella 'pages'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'user_id' già presente nella tabella 'pages'.")
            else:
                raise

        # --- Tabella users ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        # Aggiungi la colonna 'role' (se non l'abbiamo già fatto)
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        except sqlite3.OperationalError: pass # Ignora se esiste già
        # Aggiungi la nuova colonna per il dominio del widget
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN allowed_widget_domain TEXT")
            logger.info("Colonna 'allowed_widget_domain' aggiunta alla tabella 'users'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'allowed_widget_domain' già presente nella tabella 'users'.")
            else:
                raise
    

        # --- Tabella api_keys ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, -- ID univoco per la chiave stessa
                user_id TEXT NOT NULL,              -- ID dell'utente proprietario (FK verso users.id)
                key TEXT UNIQUE NOT NULL,           -- La chiave API effettiva (stringa lunga e casuale)
                name TEXT,                          -- Nome descrittivo opzionale dato dall'utente
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP NULL,         -- Opzionale: per tracciare l'ultimo uso
                is_active BOOLEAN DEFAULT TRUE,      -- Opzionale: per poter revocare una chiave
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE -- Se elimino l'utente, cancella le sue chiavi
            )''')
        logger.debug("Tabella 'api_keys' verificata/creata.")

        # --- Tabella per Canali YouTube Monitorati ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitored_youtube_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,           -- L'ID UC... del canale
                channel_url TEXT,                   -- URL fornito dall'utente (riferimento)
                channel_name TEXT,                  -- Nome (opzionale, recuperabile)
                is_active BOOLEAN DEFAULT TRUE,     -- Flag per attivare/disattivare
                last_checked_at TIMESTAMP NULL,     -- Quando lo scheduler ha controllato
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                -- Assicura che un utente monitori un canale solo una volta
                UNIQUE (user_id, channel_id)
            )''')
        logger.info("Tabella 'monitored_youtube_channels' verificata/creata.")

        # Tabella per Feed RSS Monitorati
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitored_rss_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                feed_url TEXT NOT NULL,             -- L'URL del feed RSS/Atom
                feed_title TEXT,                    -- Titolo (opzionale, recuperabile)
                is_active BOOLEAN DEFAULT TRUE,     -- Flag per attivare/disattivare
                last_checked_at TIMESTAMP NULL,     -- Quando lo scheduler ha controllato
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                 -- Assicura che un utente monitori un feed solo una volta
                UNIQUE (user_id, feed_url)
            )''')
        logger.info("Tabella 'monitored_rss_feeds' verificata/creata.")

        # --- Tabella per le Impostazioni Utente ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                llm_provider TEXT,
                llm_model_name TEXT,
                llm_embedding_model TEXT,
                llm_api_key TEXT,
                rag_temperature REAL, 
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )''')
        logger.info("Tabella 'user_settings' verificata/creata.")


        # --- Tabella per Statistiche Pre-Calcolate (Caching) ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS content_stats (
                content_id TEXT PRIMARY KEY,    -- Corrisponde a video_id, doc_id, article_id, etc.
                user_id TEXT NOT NULL,
                source_type TEXT NOT NULL,      -- 'video', 'document', 'article', 'page'
                word_count INTEGER,
                gunning_fog REAL,
                last_calculated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )''')
        logger.info("Tabella 'content_stats' per il caching delle statistiche verificata/creata.")


        # --- Tabella per Log delle Domande ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS query_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,               -- Da dove arriva la domanda (es. 'telegram', 'web_chat')
                query_text TEXT NOT NULL,           -- La domanda effettiva dell'utente finale
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        logger.info("Tabella 'query_logs' per le domande degli utenti verificata/creata.")


        # --- Aggiunta colonne per Personalizzazione ---
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN brand_color TEXT")
            logger.info("Colonna 'brand_color' aggiunta a 'user_settings'.")
        except sqlite3.OperationalError:
            logger.debug("Colonna 'brand_color' già presente in 'user_settings'.")

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN brand_logo_url TEXT")
            logger.info("Colonna 'brand_logo_url' aggiunta a 'user_settings'.")
        except sqlite3.OperationalError:
            logger.debug("Colonna 'brand_logo_url' già presente in 'user_settings'.")
            
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN welcome_message TEXT")
            logger.info("Colonna 'welcome_message' aggiunta a 'user_settings'.")
        except sqlite3.OperationalError:
            logger.debug("Colonna 'welcome_message' già presente in 'user_settings'.")

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN prompt_starter_1 TEXT")
            logger.info("Colonna 'prompt_starter_1' aggiunta a 'user_settings'.")
        except sqlite3.OperationalError:
            logger.debug("Colonna 'prompt_starter_1' già presente in 'user_settings'.")

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN prompt_starter_2 TEXT")
            logger.info("Colonna 'prompt_starter_2' aggiunta a 'user_settings'.")
        except sqlite3.OperationalError:
            logger.debug("Colonna 'prompt_starter_2' già presente in 'user_settings'.")
            
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN prompt_starter_3 TEXT")
            logger.info("Colonna 'prompt_starter_3' aggiunta a 'user_settings'.")
        except sqlite3.OperationalError:
            logger.debug("Colonna 'prompt_starter_3' già presente in 'user_settings'.")
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN brand_font TEXT")
            logger.info("Colonna 'brand_font' aggiunta a 'user_settings'.")
        except sqlite3.OperationalError:
            logger.debug("Colonna 'brand_font' già presente in 'user_settings'.")


        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN ollama_base_url TEXT")
            logger.info("Colonna 'ollama_base_url' aggiunta alla tabella 'user_settings'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'ollama_base_url' già presente in 'user_settings'.")
            else:
                raise



        # COLONNE PER WORDPRESS  ---
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN wordpress_url TEXT")
            logger.info("Colonna 'wordpress_url' aggiunta alla tabella 'user_settings'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'wordpress_url' già presente in 'user_settings'.")
            else:
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN wordpress_api_key TEXT")
            logger.info("Colonna 'wordpress_api_key' aggiunta alla tabella 'user_settings'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'wordpress_api_key' già presente in 'user_settings'.")
            else:
                raise

            
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN wordpress_username TEXT")
            logger.info("Colonna 'wordpress_username' aggiunta alla tabella 'user_settings'.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("Colonna 'wordpress_username' già presente in 'user_settings'.")
            else:
                raise  
        
        # Questo blocco sarebbe servito per Wordpress Oauth, che avrebbe peggiorato UX
        # try:
        #     cursor.execute("ALTER TABLE user_settings ADD COLUMN wordpress_access_token TEXT")
        #     logger.info("Colonna 'wordpress_access_token' aggiunta a 'user_settings'.")
        # except sqlite3.OperationalError:
        #     logger.debug("Colonna 'wordpress_access_token' già presente in 'user_settings'.")
        
        # try:
        #     cursor.execute("ALTER TABLE user_settings ADD COLUMN wordpress_refresh_token TEXT")
        #     logger.info("Colonna 'wordpress_refresh_token' aggiunta a 'user_settings'.")
        # except sqlite3.OperationalError:
        #     logger.debug("Colonna 'wordpress_refresh_token' già presente in 'user_settings'.")
            
        # try:
        #     cursor.execute("ALTER TABLE user_settings ADD COLUMN wordpress_token_expires_at INTEGER")
        #     logger.info("Colonna 'wordpress_token_expires_at' aggiunta a 'user_settings'.")
        # except sqlite3.OperationalError:
        #     logger.debug("Colonna 'wordpress_token_expires_at' già presente in 'user_settings'.")
            
        # try:
        #     cursor.execute("ALTER TABLE user_settings ADD COLUMN wordpress_blog_id TEXT")
        #     logger.info("Colonna 'wordpress_blog_id' aggiunta a 'user_settings'.")
        # except sqlite3.OperationalError:
        #     logger.debug("Colonna 'wordpress_blog_id' già presente in 'user_settings'.")
        
        conn.commit()

        # Aggiorna messaggio log finale
        logger.info(f"Database '{db_path}' inizializzato/aggiornato. Verificate/Aggiunte colonne 'user_id'. Tabella 'users' verificata/creata.")

    except sqlite3.Error as e:
        logger.error(f"Errore SQLite durante init_db ({db_path}): {e}")
        if conn: conn.rollback()
        raise
    finally:
        if conn: conn.close()