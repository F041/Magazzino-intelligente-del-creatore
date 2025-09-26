import logging
import sqlite3
import threading
import textstat
import copy
import time 
from typing import Optional
from google.api_core import exceptions as google_exceptions

from flask import current_app

from app.services.youtube.client import YouTubeClient
from app.services.transcripts.youtube_transcript import TranscriptService
from app.services.transcripts.youtube_transcript_unofficial_library import UnofficialTranscriptService
from app.services.embedding.embedding_service import generate_embeddings
from app.services.embedding.gemini_embedding import split_text_into_chunks, TASK_TYPE_DOCUMENT
from app.utils import build_full_config_for_background_process


logger = logging.getLogger(__name__)


def _process_youtube_channel_core(channel_id: str, user_id: Optional[str], core_config: dict, videos_from_yt_models: list, status_dict: dict) -> dict:
    """
    Logica centrale per processare TUTTI i video NUOVI di un canale.
    Usa il dizionario core_config per tutte le impostazioni e i client.
    Accetta una lista di video già recuperati e un dizionario di stato per l'UI.
    """
    logger.info(f"[CORE YT Process] Avvio per channel_id={channel_id}, user_id={user_id}")
    overall_success = False
    conn_sqlite = None
    processed_videos_data_for_db = []
    yt_count = len(videos_from_yt_models)
    db_existing_count, to_process_count, saved_ok_count = 0, 0, 0
    transcript_errors, embedding_errors, chroma_errors, generic_errors = 0, 0, 0, 0

    try:
        app_mode = core_config.get('APP_MODE')
        token_path = core_config.get('TOKEN_PATH')
        db_path_sqlite = core_config.get('DATABASE_FILE')
        llm_api_key = core_config.get('GOOGLE_API_KEY')
        embedding_model = core_config.get('GEMINI_EMBEDDING_MODEL')
        base_video_collection_name = core_config.get('VIDEO_COLLECTION_NAME')
        chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS')
        chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS')
        
        chroma_client_from_core_config = core_config.get('CHROMA_CLIENT')
        chroma_collection_single_from_core_config = core_config.get('CHROMA_VIDEO_COLLECTION') if app_mode == 'single' else None

        required_config_keys = {
            'APP_MODE', 'TOKEN_PATH', 'DATABASE_FILE', 'GOOGLE_API_KEY', 
            'GEMINI_EMBEDDING_MODEL', 'VIDEO_COLLECTION_NAME', 
            'DEFAULT_CHUNK_SIZE_WORDS', 'DEFAULT_CHUNK_OVERLAP_WORDS',
            'CHROMA_CLIENT' 
        }
        missing_keys = [key for key in required_config_keys if core_config.get(key) is None]
        if missing_keys:
            raise RuntimeError(f"Configurazione incompleta fornita a _process_youtube_channel_core: {', '.join(missing_keys)}")

        is_chroma_setup_ok = False
        if app_mode == 'single':
            is_chroma_setup_ok = chroma_collection_single_from_core_config is not None
        elif app_mode == 'saas':
            is_chroma_setup_ok = chroma_client_from_core_config is not None
        
        if not is_chroma_setup_ok:
             raise RuntimeError("Setup ChromaDB incompleto in core_config per la modalità operativa corrente.")
        
        logger.info(f"[CORE YT Process] Configurazione letta da core_config: OK. APP_MODE='{app_mode}'")
        
        conn_sqlite = sqlite3.connect(db_path_sqlite, timeout=10.0)
        cursor_sqlite = conn_sqlite.cursor()
        
        logger.info(f"[CORE YT Process] Ricevuti {yt_count} video totali da YouTube come argomento.")

        existing_video_ids = set()
        sql_check_existing = "SELECT video_id FROM videos WHERE channel_id = ?"
        params_check_existing = [channel_id]
        if app_mode == 'saas':
            if not user_id: raise ValueError("User ID mancante in modalità SAAS per processamento core.")
            sql_check_existing += " AND user_id = ?"
            params_check_existing.append(user_id)
        cursor_sqlite.execute(sql_check_existing, tuple(params_check_existing))
        existing_video_ids = {row[0] for row in cursor_sqlite.fetchall()}
        db_existing_count = len(existing_video_ids)
        logger.info(f"[CORE YT Process] Trovati {db_existing_count} video esistenti nel DB per questo canale/utente.")

        videos_to_process_models = [v for v in videos_from_yt_models if v.video_id not in existing_video_ids]
        to_process_count = len(videos_to_process_models)
        
        # Aggiorniamo lo stato UI con il numero di video da processare
        status_lock_ui = threading.Lock()
        with status_lock_ui:
             status_dict['total_videos'] = to_process_count

        if to_process_count == 0:
            logger.info("[CORE YT Process] Nessun nuovo video da processare."); overall_success = True
        else:
            logger.info(f"[CORE YT Process] Identificati {to_process_count} nuovi video da processare.")
            
            chroma_collection_for_upsert = None
            collection_name_for_log = "N/A"
            if app_mode == 'single':
                chroma_collection_for_upsert = chroma_collection_single_from_core_config
                if chroma_collection_for_upsert: collection_name_for_log = chroma_collection_for_upsert.name
            elif app_mode == 'saas':
                user_video_collection_name = f"{base_video_collection_name}_{user_id}"
                collection_name_for_log = user_video_collection_name
                try:
                    chroma_collection_for_upsert = chroma_client_from_core_config.get_or_create_collection(name=user_video_collection_name)
                except Exception as e_saas_coll_upsert:
                    logger.error(f"[CORE YT Process] Fallimento get/create collezione SAAS '{collection_name_for_log}': {e_saas_coll_upsert}")

            if not chroma_collection_for_upsert:
                 logger.warning(f"[CORE YT Process] Collezione Chroma '{collection_name_for_log}' NON disponibile.")

            youtube_client = YouTubeClient(token_file=token_path)

            for index, video_model in enumerate(videos_to_process_models, 1):
                with status_lock_ui:
                    status_dict['current_video'] = {'title': video_model.title, 'index': index, 'total': to_process_count}
                    status_dict['message'] = f"Processo video {index}/{to_process_count}: {video_model.title}"
                
                video_id = video_model.video_id; video_title = video_model.title
                logger.info(f"[CORE YT Process] --- Processing video {index}/{to_process_count}: {video_id} ({video_title}) ---")
                try:
                    transcript_result = None
                    last_transcript_error = "Nessun metodo di trascrizione ha avuto successo."

                    # --- TENTATIVO 1: METODO NON UFFICIALE (A COSTO ZERO) ---
                    logger.info(f"[CORE YT Process] [{video_id}] Tentativo #1: Metodo non ufficiale (a costo zero)...")
                    transcript_result = UnofficialTranscriptService.get_transcript(video_id)

                    # --- TENTATIVO 2: FALLBACK SU METODO UFFICIALE (COSTOSO) ---
                    # Lo usiamo solo se il primo tentativo non ha prodotto testo.
                    if not transcript_result or transcript_result.get('error'):
                        logger.warning(f"[CORE YT Process] [{video_id}] Metodo non ufficiale fallito. Fallback su API ufficiale (costoso)...")
                        
                        # Salviamo l'errore precedente per i log
                        last_transcript_error = transcript_result.get('message', 'Libreria non ufficiale fallita.') if transcript_result else 'Libreria non ufficiale fallita.'
                        
                        # Usiamo la logica di tentativi solo per l'API ufficiale che può avere problemi di quota
                        import time
                        for attempt in range(3):
                            logger.info(f"[CORE YT Process] [{video_id}] Tentativo API Ufficiale #{attempt + 1}...")
                            transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)
                            if transcript_result and not transcript_result.get('error'):
                                break # Successo, usciamo
                            else:
                                last_transcript_error = transcript_result.get('message', 'Errore API ufficiale') if transcript_result else 'Errore API ufficiale'
                                if "quota" in last_transcript_error.lower():
                                    logger.error(f"[CORE YT Process] [{video_id}] QUOTA API SUPERATA. Interruzione dei tentativi per questo video.")
                                    break # Inutile riprovare se la quota è finita
                                time.sleep(2)
                    
                    if transcript_result and not transcript_result.get('error'):
                        transcript_text, transcript_lang, transcript_type = transcript_result['text'], transcript_result['language'], transcript_result['type']
                        current_video_status = 'processing_embedding'
                    else: 
                        current_video_status = 'failed_transcript'; transcript_errors += 1
                        logger.error(f"[CORE YT Process] [{video_id}] Recupero trascrizione fallito. Errore finale: {last_transcript_error}")
                    
                    if current_video_status == 'processing_embedding' and transcript_text:
                        chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                        if not chunks: current_video_status = 'completed'
                    elif current_video_status != 'failed_transcript':
                        current_video_status = 'failed_transcript'; transcript_errors += 1

                    if current_video_status == 'processing_embedding' and chunks:
                        if not chroma_collection_for_upsert:
                            current_video_status = 'failed_chroma_config'; chroma_errors += 1
                        else:
                            try:
                                user_settings_for_embedding = {'llm_provider': 'google', 'llm_api_key': llm_api_key, 'llm_embedding_model': embedding_model}
                                embeddings = generate_embeddings(chunks, user_settings=user_settings_for_embedding, task_type=TASK_TYPE_DOCUMENT)
                                if embeddings and len(embeddings) == len(chunks):
                                    ids = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                                    metadatas_chroma = [{"video_id": video_id, "channel_id": video_model.channel_id, "video_title": video_model.title, "published_at": str(video_model.published_at), "chunk_index": i, "language": transcript_lang, "caption_type": transcript_type, **({"user_id": user_id} if app_mode == 'saas' and user_id else {}) } for i in range(len(chunks))]
                                    chroma_collection_for_upsert.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas_chroma, documents=chunks)
                                    current_video_status = 'completed'
                                else: current_video_status = 'failed_embedding'; embedding_errors += 1
                            except google_exceptions.ResourceExhausted: current_video_status = 'failed_embedding_ratelimit'; embedding_errors += 1
                            except google_exceptions.GoogleAPIError: current_video_status = 'failed_embedding_api'; embedding_errors += 1
                            except Exception as e_emb_chroma: 
                                current_video_status = 'failed_chroma_write' if 'upsert' in str(e_emb_chroma).lower() else 'failed_embedding'
                                if current_video_status == 'failed_chroma_write': chroma_errors +=1
                                else: embedding_errors += 1
                except Exception as e_video_proc:
                    current_video_status = 'failed_processing'; generic_errors += 1

                if current_video_status == 'completed':
                    try:
                        stats = {'word_count': 0, 'gunning_fog': 0}
                        if transcript_text and transcript_text.strip():
                            stats['word_count'] = len(transcript_text.split())
                            stats['gunning_fog'] = textstat.gunning_fog(transcript_text)
                        cursor_sqlite.execute("INSERT INTO content_stats (content_id, user_id, source_type, word_count, gunning_fog) VALUES (?, ?, ?, ?, ?) ON CONFLICT(content_id) DO UPDATE SET word_count = excluded.word_count, gunning_fog = excluded.gunning_fog, last_calculated = CURRENT_TIMESTAMP", (video_id, user_id, 'videos', stats['word_count'], stats['gunning_fog']))
                    except Exception as e_stats:
                        logger.error(f"[CORE YT Process] [{video_id}] Errore salvataggio statistiche: {e_stats}")
                
                video_data_dict = video_model.model_dump()
                video_data_dict.update({'transcript': transcript_text, 'transcript_language': transcript_lang, 'captions_type': transcript_type, 'processing_status': current_video_status, 'user_id': user_id})
                processed_videos_data_for_db.append(video_data_dict)
                logger.info(f"[CORE YT Process] --- Fine video {video_id}. Status: {current_video_status} ---")
                time.sleep(1)

            if processed_videos_data_for_db:
                try:
                    sql_insert = ''' INSERT OR REPLACE INTO videos (video_id, title, url, channel_id, published_at, description, transcript, transcript_language, captions_type, user_id, processing_status, added_at ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) '''
                    data_to_insert = [(vd['video_id'], vd['title'], vd['url'], vd['channel_id'], str(vd['published_at']), vd['description'], vd['transcript'], vd['transcript_language'], vd['captions_type'], vd['user_id'], vd.get('processing_status', 'failed')) for vd in processed_videos_data_for_db]
                    cursor_sqlite.executemany(sql_insert, data_to_insert)
                    conn_sqlite.commit()
                    saved_ok_count = len(data_to_insert)
                    overall_success = True
                except sqlite3.Error as e_sql_batch:
                    if conn_sqlite: conn_sqlite.rollback()
                    overall_success = False
    
    except Exception as e_core_generic:
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    finally:
        if conn_sqlite:
            try: conn_sqlite.close()
            except: pass 
        log_summary = (f"[CORE YT Process] Riepilogo per {channel_id}: YT Totali:{yt_count}, DB Esistenti:{db_existing_count}, Tentati:{to_process_count}, Salvati DB:{saved_ok_count}. Errori-> T:{transcript_errors}, E:{embedding_errors}, C:{chroma_errors}, G:{generic_errors}. Successo: {overall_success}")
        if overall_success: logger.info(log_summary)
        else: logger.error(log_summary)
    return {"success": overall_success, "new_videos_processed": saved_ok_count, "total_videos_on_yt": yt_count}


def _background_channel_processing(app_context, channel_url: str, user_id: Optional[str], initial_status: dict, status_dict: dict):
    """
    Esegue l'elaborazione del canale in un thread separato, chiamando la funzione core.
    Aggiorna il dizionario di stato 'status_dict' fornito per la UI.
    """
    status_lock = threading.Lock()
    thread_final_message = "Elaborazione terminata."
    job_success = False

    with status_lock:
        status_dict.update(initial_status)
        status_dict['is_processing'] = True
        status_dict['message'] = 'Avvio elaborazione...'
    logger.info(f"BACKGROUND THREAD YT: Avvio per {channel_url}, user_id={user_id}")

    with app_context:
        try:
            from app.main import load_credentials
            if not load_credentials():
                raise RuntimeError("Credenziali Google non valide o scadute.")

            token_path = current_app.config.get('TOKEN_PATH')
            youtube_client = YouTubeClient(token_file=token_path)
            channel_id_extracted = youtube_client.extract_channel_info(channel_url)
            if not channel_id_extracted:
                 raise ValueError(f"Impossibile estrarre un Channel ID da '{channel_url}'.")

            videos_list, total_count = youtube_client.get_channel_videos_and_total_count(channel_id_extracted)
            
            with status_lock:
                status_dict['total_videos_on_channel'] = total_count

            core_config_dict = build_full_config_for_background_process(user_id)
            
            result_data = _process_youtube_channel_core(
                channel_id_extracted,
                user_id,
                core_config_dict,
                videos_list,
                status_dict
            )
            job_success = result_data.get("success", False)
            new_videos_count = result_data.get("new_videos_processed", 0)

            if job_success:
                if new_videos_count > 0:
                    thread_final_message = f"Processo completato! Aggiunti {new_videos_count} nuovi video."
                else:
                    thread_final_message = "Canale già aggiornato. Nessun nuovo video trovato."
            else:
                 thread_final_message = "Si sono verificati errori durante il processo. Controllare i log."

        except Exception as e_thread:
            logger.exception(f"BACKGROUND THREAD YT: ERRORE CRITICO.")
            error_text = str(e_thread).lower()
            if 'forbidden' in error_text or '403' in error_text:
                thread_final_message = "Errore 403: Accesso negato da YouTube (possibile problema con 'Account Brand')."
            else:
                thread_final_message = f"Errore critico: {e_thread}"
            job_success = False
        finally:
            logger.info(f"BACKGROUND THREAD YT: Esecuzione finally.")
            with status_lock:
                status_dict['is_processing'] = False
                status_dict['current_video'] = None
                status_dict['message'] = thread_final_message
            logger.info(f"BACKGROUND THREAD YT: Terminato. Successo: {job_success}. Messaggio: {thread_final_message}")