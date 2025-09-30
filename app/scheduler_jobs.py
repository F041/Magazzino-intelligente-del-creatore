# FILE: app/scheduler_jobs.py (Versione Semplificata e Robusta)
import logging
import sqlite3
import traceback
# RIMOSSO: from flask import current_app
from app.utils import build_full_config_for_background_process

# Importa solo le funzioni CORE, non più create_app o AppConfig
try:
    from app.core.youtube_processor import _process_youtube_channel_core
    from .api.routes.rss import _process_rss_feed_core
except ImportError as e:
    # Gestione fallback nel caso in cui le funzioni non siano ancora disponibili
    logging.critical(f"Errore importazione funzioni CORE in scheduler_jobs: {e}")
    _process_youtube_channel_core = lambda c, u, cfg: False
    _process_rss_feed_core = lambda f, u, cfg, s, l: False


logger = logging.getLogger(__name__)

def check_monitored_sources_job():
    """
    Job eseguito periodicamente. Per ogni sorgente attiva, costruisce una configurazione
    completa che include le impostazioni personalizzate dell'utente.
    """
    logger.info("SCHEDULER JOB: Inizio esecuzione...")
    
    # --- MODIFICA CHIAVE: Importiamo e creiamo l'app QUI DENTRO ---
    from app.main import create_app
    app = create_app()
    # --- FINE MODIFICA ---

    # Usiamo un contesto app per accedere a current_app in modo sicuro
    with app.app_context():
        db_path = app.config.get('DATABASE_FILE') # <-- MODIFICA: Usiamo app.config
        if not db_path:
            logger.error("SCHEDULER JOB: Percorso del database non configurato. Interruzione.")
            return

        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            logger.info("SCHEDULER JOB: DB Connesso.")

            # --- Processa Canali YouTube ---
            cursor.execute("SELECT id, user_id, channel_id FROM monitored_youtube_channels WHERE is_active = TRUE")
            active_channels = cursor.fetchall()
            logger.info(f"Canali YT attivi trovati: {len(active_channels)}")

            channel_ids_processed = []
            for channel in active_channels:
                monitor_id, user_id, channel_id = channel['id'], channel['user_id'], channel['channel_id']
                logger.info(f"Controllo YT Canale: {channel_id} (User: {user_id})")
                try:
                    # --- MODIFICA CHIAVE: Costruisci la config completa per QUESTO utente ---
                    full_user_config = build_full_config_for_background_process(user_id)
                    
                    # Lo scheduler deve prima recuperare la lista dei video
                    from app.services.youtube.client import YouTubeClient
                    token_path = full_user_config.get('TOKEN_PATH')
                    youtube_client = YouTubeClient(token_file=token_path)
                    videos_list, _ = youtube_client.get_channel_videos_and_total_count(channel_id)

                    # Ora chiama il core con la lista dei video e il nuovo parametro
                    result_data = _process_youtube_channel_core(
                        channel_id=channel_id, 
                        user_id=user_id, 
                        core_config=full_user_config,
                        videos_from_yt_models=videos_list,
                        status_dict={}, # Lo status_dict non serve allo scheduler
                        use_official_api_only=True 
                    )
                    
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
            cursor.execute("SELECT id, user_id, feed_url FROM monitored_rss_feeds WHERE is_active = TRUE")
            active_feeds = cursor.fetchall()
            logger.info(f"Feed RSS attivi trovati: {len(active_feeds)}")
            
            feed_ids_processed = []
            for feed in active_feeds:
                monitor_id, user_id, feed_url = feed['id'], feed['user_id'], feed['feed_url']
                logger.info(f"Controllo RSS Feed: {feed_url} (User: {user_id})")
                try:
                    # --- MODIFICA CHIAVE: Costruisci la config completa per QUESTO utente ---
                    full_user_config = build_full_config_for_background_process(user_id)

                    # Ora passiamo la configurazione completa alla funzione core
                    success = _process_rss_feed_core(feed_url, user_id, full_user_config, None, None)
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