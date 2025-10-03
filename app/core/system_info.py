import os
import sqlite3
import logging
from flask import current_app
from flask_login import current_user
from datetime import datetime 
import psutil

logger = logging.getLogger(__name__)

def get_system_stats():
    """
    Raccoglie le statistiche tecniche del sistema (DB, Chroma, ecc.)
    e le restituisce in un dizionario.
    """
    db_path = current_app.config.get('DATABASE_FILE')
    user_id = current_user.id
    final_stats = {}
    conn = None

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        db_stats = {
            'sqlite_file_size_mb': 0,
            'chroma_db_folder_size_mb': 0,
            'chroma_total_chunks': 0,
            'sqlite_table_counts': {}
        }

        scheduler_stats = {
            'next_run': 'Non pianificato'
        }

        # --- blocco aggiornato per recupero scheduler (in system_info.py) ---
        try:
            scheduler = current_app.config.get('SCHEDULER_INSTANCE')

            if not scheduler:
                scheduler_stats['next_run'] = 'Scheduler non disponibile'
            elif not hasattr(scheduler, 'get_jobs'):
                logger.warning(f"Oggetto scheduler presente ma senza get_jobs(): {type(scheduler)}")
                scheduler_stats['next_run'] = 'Scheduler non interrogabile'
            else:
                try:
                    # Preferiamo cercare il job specifico se esiste (più esplicito)
                    job = None
                    try:
                        job = scheduler.get_job('check_monitored_sources_job')
                    except Exception:
                        # get_job potrebbe non essere disponibile su alcune implementazioni; fallback a get_jobs
                        job = None

                    jobs = None
                    try:
                        jobs = scheduler.get_jobs()
                    except Exception as e:
                        logger.warning(f"Impossibile leggere jobs dallo scheduler: {e}")
                        scheduler_stats['next_run'] = 'Errore nel recupero jobs'
                        jobs = None

                    if job is None and jobs:
                        # prendi il job con lo stesso id, se presente nella lista
                        for j in jobs:
                            if getattr(j, 'id', None) == 'check_monitored_sources_job':
                                job = j
                                break

                    if not job:
                        # se non c'è il job specifico, prova a scegliere il job più prossimo dalla lista
                        if not jobs:
                            scheduler_stats['next_run'] = 'Nessun job pianificato nel registro'
                        else:
                            jobs_with_times = [j for j in jobs if getattr(j, 'next_run_time', None)]
                            if jobs_with_times:
                                next_job = min(jobs_with_times, key=lambda j: j.next_run_time)
                                candidate = next_job.next_run_time
                            else:
                                candidate = None
                    else:
                        candidate = getattr(job, 'next_run_time', None)

                    # Se la next_run_time è None, proviamo a calcolarla dal trigger (se possibile)
                    if candidate is None and job is not None:
                        try:
                            # usa la timezone del scheduler se disponibile, altrimenti UTC
                            tz = getattr(scheduler, 'timezone', None)
                            from datetime import datetime, timezone
                            now = datetime.now(tz if tz else timezone.utc)
                            # trigger.get_next_fire_time(prev_fire_time, now) -> datetime or None
                            candidate = job.trigger.get_next_fire_time(None, now)
                        except Exception as e:
                            logger.debug(f"Impossibile calcolare next_run_time dal trigger: {e}")

                    if not candidate:
                        # Se ancora nulla, forniamo informazioni utili all'utente
                        scheduler_stats['next_run'] = 'Nessuna prossima esecuzione disponibile'
                    else:
                        # Normalizziamo timezone e formattiamo
                        try:
                            # Se naive, assumiamo UTC (o la timezone del scheduler)
                            if getattr(candidate, 'tzinfo', None) is None:
                                from datetime import timezone
                                if getattr(scheduler, 'timezone', None):
                                    # proviamo ad usare la timezone del scheduler se è un tzinfo
                                    candidate = candidate.replace(tzinfo=scheduler.timezone).astimezone()
                                else:
                                    candidate = candidate.replace(tzinfo=timezone.utc).astimezone()
                            else:
                                candidate = candidate.astimezone()
                            scheduler_stats['next_run'] = candidate.strftime('%d %B %Y alle %H:%M:%S')
                        except Exception as e:
                            logger.warning(f"Errore nel formattare next_run_time: {e}")
                            scheduler_stats['next_run'] = str(candidate)
                except Exception as outer_e:
                    logger.warning(f"Errore recupero jobs (outer): {outer_e}")
                    scheduler_stats['next_run'] = 'Errore nel recupero stato'
        except Exception as e:
            logger.warning(f"Impossibile recuperare lo stato dello scheduler (outermost): {e}")
            scheduler_stats['next_run'] = 'Errore nel recupero stato'
        # --- fine blocco aggiornato ---


        final_stats['scheduler_status'] = scheduler_stats
    
        # ---  BLOCCO PER LA RAM ---
        ram_stats = {
            'server_usage_percent': 'N/D',
            'app_memory_mb': 'N/D'
        }
        try:
            # 1. Recupera la RAM totale del server
            ram_info = psutil.virtual_memory()
            ram_stats['server_usage_percent'] = ram_info.percent

            # 2. Recupera la RAM usata da QUESTO processo Python
            process = psutil.Process(os.getpid())
            # process.memory_info().rss restituisce i byte usati
            memory_bytes = process.memory_info().rss
            # Convertiamo in Megabyte e arrotondiamo
            ram_stats['app_memory_mb'] = round(memory_bytes / (1024 * 1024), 2)

        except Exception as e:
            logger.warning(f"Impossibile recuperare le informazioni sulla RAM: {e}")
            # Se qualcosa va storto, i valori rimarranno 'N/D'
        
        final_stats['ram_status'] = ram_stats

        version_stats = {
            'version': 'sviluppo locale' # Valore di default
        }
        try:
            # Il percorso del file di versione all'interno del container
            version_file_path = os.path.join(current_app.config.get('BASE_DIR', '/app'), 'version.txt')
            if os.path.exists(version_file_path):
                with open(version_file_path, 'r') as f:
                    version_hash = f.read().strip()
                    # Aggiungiamo un link diretto al commit su GitHub per comodità
                    version_stats['version'] = f'<a href="https://github.com/F041/Magazzino-intelligente-del-creatore/commit/{version_hash}" target="_blank" rel="noopener noreferrer">{version_hash}</a>'
        except Exception as e:
            logger.warning(f"Impossibile leggere il file di versione: {e}")
            version_stats['version'] = 'Sconosciuta' # Errore in lettura
        
        final_stats['version_status'] = version_stats

  

        if os.path.exists(db_path):
            db_stats['sqlite_file_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
        
        chroma_path = current_app.config.get('CHROMA_PERSIST_PATH')
        if chroma_path and os.path.exists(chroma_path):
            total_size_bytes = sum(os.path.getsize(os.path.join(dirpath, f)) for dirpath, _, filenames in os.walk(chroma_path) for f in filenames if not os.path.islink(os.path.join(dirpath, f)))
            db_stats['chroma_db_folder_size_mb'] = round(total_size_bytes / (1024 * 1024), 2)
            
            MB_PER_PHOTO = 2
            db_stats['sqlite_photos_equiv'] = int(db_stats['sqlite_file_size_mb'] / MB_PER_PHOTO)
            db_stats['chroma_photos_equiv'] = int(db_stats['chroma_db_folder_size_mb'] / MB_PER_PHOTO)
            
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            if chroma_client:
                app_mode = current_app.config.get('APP_MODE', 'single')
                total_chunks = 0
                base_names = {
                    "VIDEO": "video_transcripts", "DOCUMENT": "document_content",
                    "ARTICLE": "article_content", "PAGE": "page_content"
                }
                for base_name in base_names.values():
                    coll_name = f"{base_name}_{user_id}" if app_mode == 'saas' else base_name
                    try:
                        collection = chroma_client.get_collection(name=coll_name)
                        total_chunks += collection.count()
                    except Exception:
                        pass
                db_stats['chroma_total_chunks'] = total_chunks

        sqlite_tables = ['users', 'api_keys', 'videos', 'documents', 'articles', 'pages', 'query_logs']
        for table_name in sqlite_tables:
            try:
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns = [col[1] for col in cursor.fetchall()]
                if 'user_id' in columns:
                    cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE user_id = ?", (user_id,))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                db_stats['sqlite_table_counts'][table_name] = cursor.fetchone()[0]
            except sqlite3.Error:
                db_stats['sqlite_table_counts'][table_name] = "N/D"
        
        final_stats['db_status'] = db_stats

    except sqlite3.Error as e:
        final_stats['error'] = 'Si è verificato un errore nel caricamento dei dati.'
        logger.error(f"Errore DB in get_system_stats per utente {user_id}: {e}")
    finally:
        if conn:
            conn.close()
            
    return final_stats