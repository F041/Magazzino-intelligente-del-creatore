import os
import sys
import argparse
import sqlite3
import logging
from tqdm import tqdm

# --- IMPOSTAZIONE DEL PERCORSO ---
current_script_path = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_script_path)
sys.path.append(project_root)
# --- FINE IMPOSTAZIONE PERCORSO ---

from app.main import create_app
from app.api.routes.videos import _reindex_video_from_db
from app.utils import build_full_config_for_background_process

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def reindex_videos_for_user(app, user_id: str):
    """
    Funzione che re-indicizza solo i video di un utente che non sono
    ancora stati processati con la versione di chunking desiderata e con il modello corretto.
    """
    import time 

    logger.info(f"Avvio re-indicizzazione video per l'utente ID: {user_id}")
    
    video_ids_to_process = []
    conn = None
    
    with app.app_context():
        core_config = build_full_config_for_background_process(user_id)
        db_path = core_config['DATABASE_FILE']
        
        # --- INIZIO BLOCCO MODIFICATO ---
        use_agentic = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'
        
        TARGET_CHUNK_VERSION = ''
        if use_agentic:
            rag_models = core_config.get('RAG_MODELS_LIST', [])
            model_name_marker = rag_models[0].strip() if rag_models and rag_models[0].strip() else "unknown_model"
            TARGET_CHUNK_VERSION = f'agentic_v1_{model_name_marker}'
        else:
            TARGET_CHUNK_VERSION = 'classic_v1'
            
        logger.info(f"Obiettivo di questo script: assicurarsi che tutti i video siano alla versione '{TARGET_CHUNK_VERSION}'")
        # --- FINE BLOCCO MODIFICATO ---

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute("SELECT video_id FROM videos WHERE user_id = ? AND chunking_version IS NOT ?", (user_id, TARGET_CHUNK_VERSION))
            results = cursor.fetchall()
            video_ids_to_process = [row[0] for row in results]
            
            logger.info(f"Trovati {len(video_ids_to_process)} video da aggiornare alla versione '{TARGET_CHUNK_VERSION}'.")

            if not video_ids_to_process:
                logger.info("Tutti i video sono già aggiornati. Nessuna operazione necessaria.")
                return

            with tqdm(total=len(video_ids_to_process), desc="Aggiornamento Video") as pbar:
                for video_id in video_ids_to_process:
                    pbar.set_description(f"Processo video: {video_id}")
                    try:
                        _reindex_video_from_db(video_id, conn, user_id, core_config)
                        conn.commit() 
                        pbar.update(1) 
                    except Exception as e:
                        logger.error(f"Errore durante la re-indicizzazione del video {video_id}: {e}")
                        conn.rollback()
                    finally:
                        if use_agentic:
                            time.sleep(4)
            
            logger.info(f"Aggiornamento di {len(video_ids_to_process)} video completato!")

        except sqlite3.Error as e:
            logger.error(f"Errore del database: {e}")
        finally:
            if conn:
                conn.close()

def main():
    parser = argparse.ArgumentParser(description="Script di manutenzione per il Magazzino del Creatore.")
    parser.add_argument('--email', required=True, help="L'email dell'utente di cui re-indicizzare i contenuti.")
    parser.add_argument('--type', default='videos', choices=['videos', 'documents', 'all'], help="Il tipo di contenuto da re-indicizzare.")
    
    args = parser.parse_args()

    app = create_app()
    
    user_id = None
    with app.app_context():
        db_path = app.config['DATABASE_FILE']
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (args.email,))
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
        conn.close()

    if not user_id:
        logger.error(f"Nessun utente trovato con l'email: {args.email}")
        return

    if args.type == 'videos' or args.type == 'all':
        reindex_videos_for_user(app, user_id)
    
    logger.info("Script terminato.")

if __name__ == "__main__":
    main()