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

        # ---  BLOCCO PER LO SCHEDULER ---
        scheduler_stats = {
            'next_run': 'Non pianificato',
            'last_run_status': 'N/A' # Aggiungeremo questo in futuro se serve
        }
        
        try:
            scheduler = current_app.scheduler
            job = scheduler.get_job('check_monitored_sources_job')
            if job and job.next_run_time:
                # Formattiamo la data per renderla leggibile
                next_run_dt = job.next_run_time.astimezone() # Converte nel fuso orario locale del server
                scheduler_stats['next_run'] = next_run_dt.strftime('%d %B %Y alle %H:%M:%S')
            elif job:
                scheduler_stats['next_run'] = 'In pausa o non attivo'
        except Exception as e:
            logger.warning(f"Impossibile recuperare lo stato dello scheduler: {e}")
            scheduler_stats['next_run'] = 'Errore nel recupero stato'
        
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