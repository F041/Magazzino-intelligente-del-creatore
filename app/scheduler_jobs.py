# FILE: app/scheduler_jobs.py (Versione Semplificata e Robusta)
import logging
import sqlite3
import traceback
from flask import current_app

# Importa solo le funzioni CORE, non più create_app o AppConfig
try:
    from .api.routes.videos import _process_youtube_channel_core
    from .api.routes.rss import _process_rss_feed_core
except ImportError as e:
    # Gestione fallback nel caso in cui le funzioni non siano ancora disponibili
    logging.critical(f"Errore importazione funzioni CORE in scheduler_jobs: {e}")
    _process_youtube_channel_core = lambda c, u, cfg: False
    _process_rss_feed_core = lambda f, u, cfg, s, l: False


logger = logging.getLogger(__name__)

def check_monitored_sources_job():
    """
    Job eseguito periodicamente. Si affida al contesto dell'applicazione
    fornito da APScheduler per accedere a configurazione e servizi.
    """
    # APScheduler, se inizializzato con `app=app`, esegue questo job
    # all'interno di un contesto applicativo. Quindi possiamo usare `current_app`.
    logger.info("SCHEDULER JOB: Inizio esecuzione...")

    # 1. Prepara il dizionario di configurazione direttamente da `current_app.config`
    try:
        core_config = {
            'APP_MODE': current_app.config.get('APP_MODE', 'single'),
            'DATABASE_FILE': current_app.config.get('DATABASE_FILE'),
            'TOKEN_PATH': current_app.config.get('TOKEN_PATH'),
            'ARTICLES_FOLDER_PATH': current_app.config.get('ARTICLES_FOLDER_PATH'),
            'GOOGLE_API_KEY': current_app.config.get('GOOGLE_API_KEY'),
            'GEMINI_EMBEDDING_MODEL': current_app.config.get('GEMINI_EMBEDDING_MODEL'),
            'DEFAULT_CHUNK_SIZE_WORDS': current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS'),
            'DEFAULT_CHUNK_OVERLAP_WORDS': current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS'),
            'CHROMA_CLIENT': current_app.config.get('CHROMA_CLIENT'),
            'VIDEO_COLLECTION_NAME': current_app.config.get('VIDEO_COLLECTION_NAME'),
            'ARTICLE_COLLECTION_NAME': current_app.config.get('ARTICLE_COLLECTION_NAME'),
            'DOCUMENT_COLLECTION_NAME': current_app.config.get('DOCUMENT_COLLECTION_NAME'),
            'CHROMA_VIDEO_COLLECTION': current_app.config.get('CHROMA_VIDEO_COLLECTION') # Aggiunto per completezza
        }
        # Verifica rapida
        if not core_config.get('DATABASE_FILE') or not core_config.get('CHROMA_CLIENT'):
            raise ValueError("Configurazione DB o Chroma Client mancante.")
        logger.info("SCHEDULER JOB: Dizionario 'core_config' preparato con successo.")
    except Exception as e_cfg:
        logger.error(f"SCHEDULER JOB: Errore durante la preparazione della configurazione: {e_cfg}", exc_info=True)
        return

    # 2. Esegui il lavoro
    db_path = core_config.get('DATABASE_FILE')
    conn = None
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
            monitor_id, user_id, channel_id = channel['id'], channel['user_id'], channel['channel_id']
            logger.info(f"Controllo YT Canale: {channel_id} (User: {user_id})")
            try:
                # La funzione core si aspetta solo 3 argomenti
                result_data = _process_youtube_channel_core(channel_id, user_id, core_config)
                if result_data.get("success", False):
                    channel_ids_processed.append(monitor_id)
                    logger.info(f"Processo Canale {channel_id} completato.")
                else:
                    logger.error(f"Fallimento processo core canale {channel_id}.")
            except Exception as e_ch_proc:
                logger.error(f"Errore durante chiamata _process_youtube_channel_core per {channel_id}: {e_ch_proc}\n{traceback.format_exc()}")

        if channel_ids_processed:
            placeholders = ','.join('?' * len(channel_ids_processed))
            cursor.execute(f"UPDATE monitored_youtube_channels SET last_checked_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})", channel_ids_processed)
            conn.commit()
            logger.info(f"Aggiornato last_checked_at per {len(channel_ids_processed)} canali YouTube.")

        # --- Processa Feed RSS ---
        feed_ids_processed = []
        for feed in active_feeds:
            monitor_id, user_id, feed_url = feed['id'], feed['user_id'], feed['feed_url']
            logger.info(f"Controllo RSS Feed: {feed_url} (User: {user_id})")
            try:
                # La funzione core si aspetta 5 argomenti, gli ultimi 2 sono per la UI, quindi passiamo None
                success = _process_rss_feed_core(feed_url, user_id, core_config, None, None)
                if success:
                    feed_ids_processed.append(monitor_id)
                    logger.info(f"Processo Feed {feed_url} completato.")
                else:
                    logger.error(f"Fallimento processo core feed {feed_url}.")
            except Exception as e_fd_proc:
                logger.error(f"Errore durante chiamata _process_rss_feed_core per {feed_url}: {e_fd_proc}\n{traceback.format_exc()}")
        
        if feed_ids_processed:
            placeholders = ','.join('?' * len(feed_ids_processed))
            cursor.execute(f"UPDATE monitored_rss_feeds SET last_checked_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})", feed_ids_processed)
            conn.commit()
            logger.info(f"Aggiornato last_checked_at per {len(feed_ids_processed)} feed RSS.")

        logger.info("SCHEDULER JOB: Controllo sorgenti terminato.")

    except Exception as e_job:
        logger.error(f"SCHEDULER JOB: Errore imprevisto nella logica principale: {e_job}", exc_info=True)
    finally:
        if conn:
            conn.close()
            logger.debug("SCHEDULER JOB: Connessione DB chiusa.")