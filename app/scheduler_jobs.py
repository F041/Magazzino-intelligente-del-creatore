# FILE: app/scheduler_jobs.py (CORRETTO)
import logging
import sqlite3
from flask import current_app # Serve ancora qui per creare il dizionario config
import traceback # Aggiunto per loggare traceback completi

# Importa create_app, AppConfig, e le funzioni CORE
try:
    # Importa create_app E AppConfig direttamente
    from .main import create_app, AppConfig
except ImportError as e:
    logging.critical(f"Errore critico importando create_app/AppConfig: {e}")
    # Potrebbe essere necessario un fallback o uscire
    raise # Rilancia l'errore per fermare l'avvio se questo fallisce

try:
    from .api.routes.videos import _process_youtube_channel_core
except ImportError:
    # Se l'import fallisce, definisci una funzione fittizia che logga l'errore
    def _process_youtube_channel_core(c, u, cfg):
        logging.error(f"Funzione _process_youtube_channel_core mancante! (Chiamata per canale: {c})")
        return False
try:
    from .api.routes.rss import _process_rss_feed_core
except ImportError:
    def _process_rss_feed_core(f, u, cfg):
        logging.error(f"Funzione _process_rss_feed_core mancante! (Chiamata per feed: {f})")
        return False

logger = logging.getLogger(__name__)

def check_monitored_sources_job():
    logger.info("SCHEDULER JOB: Inizio esecuzione...")
    if not AppConfig:
        logger.error("Configurazione AppConfig globale mancante. Impossibile procedere.")
        return

    core_config = None # Inizializza a None per verificare dopo

    try:
        # Crea un'istanza temporanea dell'app per ottenere il contesto e la config
        app_instance = create_app(AppConfig)
    except Exception as e:
        logger.error(f"SCHEDULER JOB: Errore creazione istanza app Flask nel job: {e}", exc_info=True)
        return # Esce se non può creare l'app

    # --- CREA DIZIONARIO CONFIG DA PASSARE (nel contesto dell'app appena creata) ---
    with app_instance.app_context():
        try:
            logger.info("SCHEDULER JOB: Contesto ottenuto per preparazione config.")
            core_config = {
                # --- Chiavi di configurazione necessarie alle funzioni CORE ---
                # Generale
                'APP_MODE': current_app.config.get('APP_MODE', 'single'),
                'DATABASE_FILE': current_app.config.get('DATABASE_FILE'),
                # Google / LLM
                'GOOGLE_API_KEY': current_app.config.get('GOOGLE_API_KEY'),
                'GEMINI_EMBEDDING_MODEL': current_app.config.get('GEMINI_EMBEDDING_MODEL'),
                # YouTube Specifiche
                'TOKEN_PATH': current_app.config.get('TOKEN_PATH'),
                # RSS Specifiche
                'ARTICLES_FOLDER_PATH': current_app.config.get('ARTICLES_FOLDER_PATH'),
                # Embedding / Chunking
                'DEFAULT_CHUNK_SIZE_WORDS': current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS'),
                'DEFAULT_CHUNK_OVERLAP_WORDS': current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS'),
                # ChromaDB (Passiamo client e nomi base, le funzioni core gestiranno le collezioni)
                'CHROMA_CLIENT': current_app.config.get('CHROMA_CLIENT'),
                'VIDEO_COLLECTION_NAME': current_app.config.get('VIDEO_COLLECTION_NAME'),
                'ARTICLE_COLLECTION_NAME': current_app.config.get('ARTICLE_COLLECTION_NAME'),
                'DOCUMENT_COLLECTION_NAME': current_app.config.get('DOCUMENT_COLLECTION_NAME'), # Anche se non usata qui, per completezza
                # Aggiungi altre chiavi se le funzioni core ne richiedono altre
            }
            # Verifica che i valori essenziali non siano None
            required_keys = ['DATABASE_FILE', 'TOKEN_PATH', 'GOOGLE_API_KEY', 'CHROMA_CLIENT'] # Esempio chiavi critiche
            missing_keys = [k for k in required_keys if not core_config.get(k)]
            if missing_keys:
                 raise ValueError(f"Valori mancanti nella config per il job: {', '.join(missing_keys)}")
            logger.info("SCHEDULER JOB: Dizionario 'core_config' preparato con successo.")

        except Exception as e_cfg:
             logger.error(f"SCHEDULER JOB: Errore durante lettura/preparazione config nel contesto: {e_cfg}", exc_info=True)
             # Non possiamo procedere senza una config valida
             return
        # --- FINE CREAZIONE DIZIONARIO CONFIG ---

    # Verifica che core_config sia stato creato prima di procedere
    if core_config is None:
        logger.error("SCHEDULER JOB: Dizionario 'core_config' non creato. Interruzione job.")
        return

    # --- ESEGUI IL LAVORO EFFETTIVO (IN UN NUOVO CONTESTO) ---
    # Usiamo un nuovo contesto per le operazioni DB e le chiamate alle funzioni core
    with app_instance.app_context():
        logger.info("SCHEDULER JOB: Contesto ottenuto per esecuzione logica principale.")
        db_path = core_config.get('DATABASE_FILE') # Prendi path da config preparata
        if not db_path:
            logger.error("SCHEDULER JOB: Path DB non trovato nel dizionario core_config!")
            return

        conn = None
        # === INIZIO BLOCCO TRY PRINCIPALE DEL JOB ===
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            logger.info("SCHEDULER JOB: DB Connesso.")

            # --- Trova Sorgenti Attive ---
            cursor.execute("SELECT id, user_id, channel_id FROM monitored_youtube_channels WHERE is_active = TRUE")
            active_channels = cursor.fetchall()
            logger.info(f"Canali YT attivi trovati: {len(active_channels)}")

            cursor.execute("SELECT id, user_id, feed_url FROM monitored_rss_feeds WHERE is_active = TRUE")
            active_feeds = cursor.fetchall()
            logger.info(f"Feed RSS attivi trovati: {len(active_feeds)}")

            # --- Processa Canali YouTube ---
            channel_ids_processed = []
            for channel in active_channels:
                monitor_id = channel['id']; user_id = channel['user_id']; channel_id = channel['channel_id']
                logger.info(f"Controllo YT Canale: {channel_id} (User: {user_id})")
                try:
                    # <<<<<<< CORREZIONE QUI >>>>>>>
                    success = _process_youtube_channel_core(channel_id, user_id, core_config)
                    # <<<<<<< /CORREZIONE QUI >>>>>>>
                    if success:
                        channel_ids_processed.append(monitor_id)
                        logger.info(f"Processo Canale {channel_id} completato con successo.")
                    else:
                        logger.error(f"Fallimento processo core canale {channel_id}.")
                except Exception as e_ch_proc:
                    # Logga l'errore completo incluso il traceback
                    logger.error(f"Errore durante chiamata _process_youtube_channel_core per {channel_id}: {e_ch_proc}\n{traceback.format_exc()}")

            # Aggiorna last_checked_at per i canali processati con successo (o tutti?)
            # Decidiamo di aggiornarlo solo se `success` è True dalla funzione core.
            if channel_ids_processed:
                 placeholders = ','.join('?' * len(channel_ids_processed))
                 cursor.execute(f"UPDATE monitored_youtube_channels SET last_checked_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})", channel_ids_processed)
                 conn.commit() # Commit dopo l'update
                 logger.info(f"Aggiornato last_checked_at per {len(channel_ids_processed)} canali YouTube.")

            # --- Processa Feed RSS ---
            feed_ids_processed = []
            for feed in active_feeds:
                monitor_id = feed['id']; user_id = feed['user_id']; feed_url = feed['feed_url']
                logger.info(f"Controllo RSS Feed: {feed_url} (User: {user_id})")
                try:
                    # <<<<<<< CORREZIONE QUI >>>>>>>
                    success = _process_rss_feed_core(feed_url, user_id, core_config)
                    # <<<<<<< /CORREZIONE QUI >>>>>>>
                    if success:
                        feed_ids_processed.append(monitor_id)
                        logger.info(f"Processo Feed {feed_url} completato con successo.")
                    else:
                        logger.error(f"Fallimento processo core feed {feed_url}.")
                except Exception as e_fd_proc:
                    # Logga l'errore completo incluso il traceback
                    logger.error(f"Errore durante chiamata _process_rss_feed_core per {feed_url}: {e_fd_proc}\n{traceback.format_exc()}")

            # Aggiorna last_checked_at per i feed processati con successo
            if feed_ids_processed:
                placeholders = ','.join('?' * len(feed_ids_processed))
                cursor.execute(f"UPDATE monitored_rss_feeds SET last_checked_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})", feed_ids_processed)
                conn.commit() # Commit dopo l'update
                logger.info(f"Aggiornato last_checked_at per {len(feed_ids_processed)} feed RSS.")

            logger.info("SCHEDULER JOB: Controllo sorgenti terminato.")

        # === BLOCCHI EXCEPT e FINALLY CORRETTI ===
        except sqlite3.Error as e_sql:
            logger.error(f"SCHEDULER JOB: Errore DB durante operazioni principali: {e_sql}", exc_info=True)
            # Non serve rollback qui se i commit sono atomici per update singolo
        except Exception as e_job:
            logger.error(f"SCHEDULER JOB: Errore imprevisto nella logica principale: {e_job}", exc_info=True)
        finally:
            if conn:
                try:
                    conn.close()
                    logger.debug("SCHEDULER JOB: Connessione DB chiusa.")
                except Exception as e_close:
                    logger.error(f"Errore chiusura connessione DB nel finally: {e_close}")
        # === FINE BLOCCO TRY/EXCEPT/FINALLY ===

    # === FINE BLOCCO WITH CONTESTO PRINCIPALE ===
    logger.debug("SCHEDULER JOB: Contesto esecuzione principale rilasciato.")