import os
import sqlite3
import logging
import json
from flask import current_app
from flask_login import current_user
from datetime import datetime 
import psutil

from app.core.setup import load_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def _get_youtube_quota_info():
    """
    Contatta la Service Usage API di Google per ottenere l'utilizzo della quota
    della YouTube Data API v3 per il progetto corrente.
    Restituisce un dizionario con le informazioni o un messaggio di errore.
    """
    try:
        credentials = load_credentials()
        if not credentials or not credentials.valid:
            return {'error': "Credenziali Google non valide o assenti. <a href='/authorize' style='font-weight: bold; text-decoration: underline;'>Clicca qui per autorizzare l'applicazione</a>."}

        project_id = None
        secrets_path = current_app.config.get('CLIENT_SECRETS_PATH')
        if os.path.exists(secrets_path):
            with open(secrets_path, 'r') as f:
                secrets = json.load(f)
                project_id = secrets.get('installed', {}).get('project_id') or secrets.get('web', {}).get('project_id')
        
        if not project_id:
            return {'error': "Impossibile determinare il project_id dal file client_secrets.json."}

        service = build('serviceusage', 'v1', credentials=credentials)
        resource_name = f"projects/{project_id}/services/youtube.googleapis.com"
        
        request = service.services().get(name=resource_name)
        response = request.execute()
        
        quota_metrics = response.get('quota', {}).get('metrics', [])
        usage_value = 0
        limit_value = 10000

        for metric in quota_metrics:
            if metric.get('name') == 'youtube.googleapis.com/default':
                limit_info = metric.get('consumerQuotaLimits', [{}])[0]
                limit_value = int(limit_info.get('quotaBuckets', [{}])[0].get('effectiveLimit', 10000))
                usage_value = int(limit_info.get('quotaBuckets', [{}])[0].get('values', {}).get('INT64', 0))
                break
        
        percentage = round((usage_value / limit_value) * 100, 2) if limit_value > 0 else 0

        return {
            'limit': limit_value,
            'usage': usage_value,
            'percentage': percentage,
            'resets_at': "09:00 (ora italiana, circa)"
        }

    except HttpError as e:
        # --- BLOCCO INTELLIGENTE PER LA GESTIONE ERRORI ---
        try:
            error_content = json.loads(e.content)
            error_details = error_content.get('error', {})
            
            # Controlliamo specificamente l'errore di PERMESSI INSUFFICIENTI
            if error_details.get('status') == 'PERMISSION_DENIED' and 'insufficient authentication scopes' in error_details.get('message', ''):
                logger.warning("Rilevato errore di scope insufficiente per la quota API.")
                # Restituiamo un messaggio HTML con il link per la soluzione!
                return {'error': "Permessi insufficienti per leggere la quota. <a href='/authorize' style='font-weight: bold; text-decoration: underline;'>Clicca qui per aggiornare l'autorizzazione</a>."}

            if 'Service Usage API has not been used' in error_details.get('message', ''):
                return {'error': "L'API 'Service Usage' non è abilitata per questo progetto Google Cloud. Abilitala e riprova."}
        except Exception:
            pass # Se non riusciamo a leggere i dettagli, usiamo il messaggio generico sotto
        
        logger.error(f"Errore HTTP nel recuperare la quota YouTube: {e}")
        return {'error': f"Errore API Google ({e.resp.status})."}
    except Exception as e:
        logger.error(f"Errore imprevisto nel recuperare la quota YouTube: {e}", exc_info=True)
        return {'error': f"Errore imprevisto: {e}"}


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

        try:
            scheduler = current_app.config.get('SCHEDULER_INSTANCE')

            if not scheduler:
                scheduler_stats['next_run'] = 'Scheduler non disponibile'
            elif not hasattr(scheduler, 'get_jobs'):
                logger.warning(f"Oggetto scheduler presente ma senza get_jobs(): {type(scheduler)}")
                scheduler_stats['next_run'] = 'Scheduler non interrogabile'
            else:
                try:
                    job = None
                    try:
                        job = scheduler.get_job('check_monitored_sources_job')
                    except Exception:
                        job = None

                    jobs = None
                    try:
                        jobs = scheduler.get_jobs()
                    except Exception as e:
                        logger.warning(f"Impossibile leggere jobs dallo scheduler: {e}")
                        scheduler_stats['next_run'] = 'Errore nel recupero jobs'
                        jobs = None

                    if job is None and jobs:
                        for j in jobs:
                            if getattr(j, 'id', None) == 'check_monitored_sources_job':
                                job = j
                                break

                    if not job:
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

                    if candidate is None and job is not None:
                        try:
                            tz = getattr(scheduler, 'timezone', None)
                            from datetime import datetime, timezone
                            now = datetime.now(tz if tz else timezone.utc)
                            candidate = job.trigger.get_next_fire_time(None, now)
                        except Exception as e:
                            logger.debug(f"Impossibile calcolare next_run_time dal trigger: {e}")

                    if not candidate:
                        scheduler_stats['next_run'] = 'Nessuna prossima esecuzione disponibile'
                    else:
                        try:
                            if getattr(candidate, 'tzinfo', None) is None:
                                from datetime import timezone
                                if getattr(scheduler, 'timezone', None):
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
        
        final_stats['scheduler_status'] = scheduler_stats
    
        ram_stats = {
            'server_usage_percent': 'N/D',
            'app_memory_mb': 'N/D'
        }
        try:
            ram_info = psutil.virtual_memory()
            ram_stats['server_usage_percent'] = ram_info.percent
            process = psutil.Process(os.getpid())
            memory_bytes = process.memory_info().rss
            ram_stats['app_memory_mb'] = round(memory_bytes / (1024 * 1024), 2)
        except Exception as e:
            logger.warning(f"Impossibile recuperare le informazioni sulla RAM: {e}")
        
        final_stats['ram_status'] = ram_stats

        version_stats = {
            'version': 'sviluppo locale'
        }
        try:
            version_file_path = os.path.join(current_app.config.get('BASE_DIR', '/app'), 'version.txt')
            if os.path.exists(version_file_path):
                with open(version_file_path, 'r') as f:
                    version_hash = f.read().strip()
                    version_stats['version'] = f'<a href="https://github.com/F041/Magazzino-intelligente-del-creatore/commit/{version_hash}" target="_blank" rel="noopener noreferrer">{version_hash}</a>'
        except Exception as e:
            logger.warning(f"Impossibile leggere il file di versione: {e}")
            version_stats['version'] = 'Sconosciuta'
        
        final_stats['version_status'] = version_stats

                # --- Recupero Avvisi di Sistema ---
        system_alerts = []
        try:
            cursor.execute("SELECT alert_type, message, created_at FROM system_alerts ORDER BY created_at DESC LIMIT 10")
            rows = cursor.fetchall()
            for row in rows:
                # Formattiamo la data in modo più leggibile (es. togliendo i millisecondi se presenti)
                dt_str = row[2]
                try:
                    # Tentativo di parsing per formattazione europea
                    dt_obj = datetime.strptime(dt_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    dt_str = dt_obj.strftime("%d/%m %H:%M")
                except: pass
                
                system_alerts.append({
                    'type': row[0],
                    'message': row[1],
                    'time': dt_str
                })
        except sqlite3.Error as e:
            logger.warning(f"Impossibile leggere system_alerts: {e}")
        
        final_stats['alerts'] = system_alerts

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
                total_chunks = 0
                base_names = {
                    "VIDEO": "video_transcripts", "DOCUMENT": "document_content",
                    "ARTICLE": "article_content", "PAGE": "page_content"
                }
                for base_name in base_names.values():
                    coll_name = f"{base_name}_{user_id}"
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
        
        final_stats['youtube_quota'] = _get_youtube_quota_info()

    except sqlite3.Error as e:
        final_stats['error'] = 'Si è verificato un errore nel caricamento dei dati.'
        logger.error(f"Errore DB in get_system_stats per utente {user_id}: {e}")
    finally:
        if conn:
            conn.close()
            
    return final_stats