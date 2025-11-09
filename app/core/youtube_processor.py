# FILE: app/core/youtube_processor.py

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
from app.services.chunking.agentic_chunker import chunk_text_agentically
from app.utils import build_full_config_for_background_process


logger = logging.getLogger(__name__)


def _process_youtube_channel_core(channel_id: str, user_id: Optional[str], core_config: dict, videos_from_yt_models: list, status_dict: dict, use_official_api_only: bool = False) -> dict:
    logger.info(f"[CORE YT Process] Avvio per channel_id={channel_id}, user_id={user_id}, Solo API Ufficiale: {use_official_api_only}")
    
    if not user_id:
        logger.error("[CORE YT Process] Errore critico: User ID non fornito. Operazione annullata.")
        return {"success": False, "error": "User ID mancante."}

    overall_success = False
    conn_sqlite = None
    processed_videos_data_for_db = []
    yt_count = len(videos_from_yt_models)
    to_process_count = 0
    saved_ok_count = 0
    transcript_errors, embedding_errors, chroma_errors, generic_errors = 0, 0, 0, 0

    try:
        token_path = core_config.get('TOKEN_PATH')
        db_path_sqlite = core_config.get('DATABASE_FILE')
        base_video_collection_name = core_config.get('VIDEO_COLLECTION_NAME')
        chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS')
        chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS')
        chroma_client_from_core_config = core_config.get('CHROMA_CLIENT')
        
        conn_sqlite = sqlite3.connect(db_path_sqlite, timeout=10.0)
        cursor_sqlite = conn_sqlite.cursor()
        
        existing_video_ids = set()
        sql_check_existing = "SELECT video_id FROM videos WHERE channel_id = ? AND user_id = ?"
        cursor_sqlite.execute(sql_check_existing, (channel_id, user_id))
        existing_video_ids = {row[0] for row in cursor_sqlite.fetchall()}
        
        videos_to_process_models = [v for v in videos_from_yt_models if v.video_id not in existing_video_ids]
        to_process_count = len(videos_to_process_models)
        
        status_lock_ui = threading.Lock()
        with status_lock_ui:
             status_dict['total_videos'] = to_process_count

        if to_process_count == 0:
            logger.info("[CORE YT Process] Nessun nuovo video da processare."); overall_success = True
        else:
            chroma_collection_for_upsert = None
            user_video_collection_name = f"{base_video_collection_name}_{user_id}"
            try:
                chroma_collection_for_upsert = chroma_client_from_core_config.get_or_create_collection(name=user_video_collection_name)
            except Exception as e:
                logger.error(f"Impossibile creare/accedere alla collezione ChromaDB '{user_video_collection_name}': {e}")
                raise RuntimeError(f"Errore ChromaDB: {e}")

            youtube_client = YouTubeClient(token_file=token_path)

            for index, video_model in enumerate(videos_to_process_models, 1):
                video_id = video_model.video_id
                with status_lock_ui:
                    status_dict.update({
                        'current_video': {'title': video_model.title, 'index': index, 'total': to_process_count},
                        'message': f"Processo video {index}/{to_process_count}: {video_model.title}",
                        'indeterminate_step': False
                    })

                transcript_text, transcript_lang, transcript_type = None, None, None
                current_video_status = 'pending'
                
                try:
                    transcript_result = None
                    if use_official_api_only:
                        transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)
                    else:
                        transcript_result = UnofficialTranscriptService.get_transcript(video_id)
                        if not transcript_result or transcript_result.get('error'):
                            if transcript_result and transcript_result.get('error') == 'IP_BLOCKED':
                                with status_lock_ui: status_dict['message'] = f"⚠️ Blocco IP! Uso API ufficiale... ({index}/{to_process_count})"
                            transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)

                    if transcript_result and not transcript_result.get('error'):
                        transcript_text, transcript_lang, transcript_type = transcript_result['text'], transcript_result['language'], transcript_result['type']
                        current_video_status = 'processing_embedding'
                    else:
                        current_video_status = 'failed_transcript'; transcript_errors += 1
                        error_msg = transcript_result.get('message', 'Errore recupero trascrizione.') if transcript_result else 'Errore sconosciuto'
                        logger.error(f"[{video_id}] Fallimento trascrizione: {error_msg}")
                
                    if current_video_status == 'processing_embedding' and transcript_text:
                        use_agentic_chunking = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'
                        chunks = []

                        if use_agentic_chunking:
                            with status_lock_ui:
                                status_dict['message'] = f"({index}/{to_process_count}) Frammentazione semantica (il processo più lungo)..."
                                status_dict['indeterminate_step'] = True
                            try:
                                chunks = chunk_text_agentically(transcript_text, llm_provider=core_config.get('llm_provider', 'google'), settings=core_config)
                            except google_exceptions.ResourceExhausted as e:
                                chunks = []
                            finally:
                                with status_lock_ui: status_dict['indeterminate_step'] = False
                            
                            if not chunks:
                                chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                        else:
                            chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

                        if not chunks: current_video_status = 'completed'
                    
                    elif current_video_status != 'failed_transcript':
                        current_video_status = 'failed_transcript'; transcript_errors += 1

                    if current_video_status == 'processing_embedding' and chunks:
                        with status_lock_ui:
                            status_dict['message'] = f"({index}/{to_process_count}) Creazione embeddings per '{video_model.title[:30]}...'"
                        
                        user_settings_for_embedding = build_full_config_for_background_process(user_id)
                        embeddings = generate_embeddings(chunks, user_settings=user_settings_for_embedding, task_type=TASK_TYPE_DOCUMENT)
                        
                        if embeddings and len(embeddings) == len(chunks):
                            ids = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                            metadatas = [{"video_id": video_id, "channel_id": video_model.channel_id, "video_title": video_model.title, "published_at": str(video_model.published_at), "chunk_index": i, "language": transcript_lang, "caption_type": transcript_type, "user_id": user_id } for i in range(len(chunks))]
                            chroma_collection_for_upsert.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=chunks)
                            current_video_status = 'completed'
                        else:
                            current_video_status = 'failed_embedding'; embedding_errors += 1
                
                except Exception as e_video_proc:
                    current_video_status = 'failed_processing'; generic_errors += 1

                video_data_dict = video_model.model_dump()
                video_data_dict.update({'transcript': transcript_text, 'transcript_language': transcript_lang, 'captions_type': transcript_type, 'processing_status': current_video_status, 'user_id': user_id})
                processed_videos_data_for_db.append(video_data_dict)

            if processed_videos_data_for_db:
                sql_insert = ''' INSERT OR REPLACE INTO videos (video_id, title, url, channel_id, published_at, description, transcript, transcript_language, captions_type, user_id, processing_status, added_at ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) '''
                data_to_insert = [(vd['video_id'], vd['title'], vd['url'], vd['channel_id'], str(vd['published_at']), vd['description'], vd['transcript'], vd['transcript_language'], vd['captions_type'], vd['user_id'], vd.get('processing_status', 'failed')) for vd in processed_videos_data_for_db]
                cursor_sqlite.executemany(sql_insert, data_to_insert)
                conn_sqlite.commit()
                saved_ok_count = len(data_to_insert)
                overall_success = True
    
    except Exception as e:
        logger.error(f"Errore critico in _process_youtube_channel_core: {e}", exc_info=True)
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    finally:
        if conn_sqlite: conn_sqlite.close()

    return {"success": overall_success, "new_videos_processed": saved_ok_count, "total_videos_on_yt": yt_count}


def _background_channel_processing(app_context, channel_url: str, user_id: Optional[str], initial_status: dict, status_dict: dict):
    status_lock = threading.Lock()
    thread_final_message = "Elaborazione terminata."
    job_success = False
    error_code_to_frontend = None # <-- NUOVA VARIABILE

    with status_lock:
        status_dict.update(initial_status)
    logger.info(f"BACKGROUND THREAD YT: Avvio per {channel_url}, user_id={user_id}")

    with app_context:
        try:
            from app.main import load_credentials
            if not load_credentials():
                # --- INIZIO BLOCCO MODIFICATO ---
                # Questo è il punto in cui l'errore viene generato. Lo catturiamo qui.
                raise RuntimeError("Credenziali Google non valide o scadute.")
                # --- FINE BLOCCO MODIFICATO ---

            youtube_client = YouTubeClient(token_file=current_app.config.get('TOKEN_PATH'))
            
            channel_id_extracted = None
            db_path = current_app.config.get('DATABASE_FILE')
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT channel_id FROM monitored_youtube_channels WHERE channel_url = ? AND user_id = ?", (channel_url, user_id))
                result = cursor.fetchone()
                if result: channel_id_extracted = result[0]
            finally:
                if conn: conn.close()

            if not channel_id_extracted:
                channel_id_extracted = youtube_client.extract_channel_info(channel_url)

            if not channel_id_extracted:
                 raise ValueError(f"Impossibile estrarre un Channel ID da '{channel_url}'.")

            videos_list, total_count = youtube_client.get_channel_videos_and_total_count(channel_id_extracted)
            
            with status_lock:
                status_dict['total_videos_on_channel'] = total_count

            core_config_dict = build_full_config_for_background_process(user_id)
            
            result_data = _process_youtube_channel_core(
                channel_id_extracted, user_id, core_config_dict, videos_list, status_dict, use_official_api_only=False 
            )

            job_success = result_data.get("success", False)
            new_videos_count = result_data.get("new_videos_processed", 0)

            if job_success:
                thread_final_message = f"Processo completato! Aggiunti {new_videos_count} nuovi video." if new_videos_count > 0 else "Canale già aggiornato. Nessun nuovo video trovato."
            else:
                 thread_final_message = "Si sono verificati errori durante il processo. Controllare i log."

        # --- INIZIO BLOCCO MODIFICATO ---
        except RuntimeError as e_runtime:
            # Catturiamo SPECIFICAMENTE l'errore delle credenziali
            logger.warning(f"BACKGROUND THREAD YT: Errore di credenziali rilevato: {e_runtime}")
            thread_final_message = "Autorizzazione Google richiesta per procedere."
            error_code_to_frontend = 'REAUTH_REQUIRED' # Il nostro "segnale" per il frontend
            job_success = False
        except google_exceptions.ResourceExhausted as e_quota:
            logger.error(f"BACKGROUND THREAD YT: QUOTA API ESAURITA! Dettagli: {e_quota}")
            thread_final_message = "Limite richieste API raggiunto. Per canali grandi, considera di usare l'automazione per processare i contenuti un po' alla volta."
            error_code_to_frontend = 'QUOTA_EXCEEDED_SUGGEST_SCHEDULE'
            job_success = False
        # --- FINE BLOCCO MODIFICATO ---
        except Exception as e_thread:
            logger.exception(f"BACKGROUND THREAD YT: ERRORE CRITICO.")
            error_text = str(e_thread).lower()
            if 'forbidden' in error_text or '403' in error_text:
                thread_final_message = "Errore Quota/Permessi API: hai esaurito le richieste giornaliere a YouTube o i permessi non sono sufficienti. Riprova domani o ri-autorizza l'applicazione."
            else:
                thread_final_message = f"Errore critico: {e_thread}"
            job_success = False
        finally:
            with status_lock:
                status_dict['is_processing'] = False
                status_dict['current_video'] = None
                status_dict['message'] = thread_final_message
                status_dict['indeterminate_step'] = False
                if error_code_to_frontend:
                    status_dict['error'] = thread_final_message
                    status_dict['error_code'] = error_code_to_frontend
                elif not job_success:
                    status_dict['error'] = thread_final_message

            logger.info(f"BACKGROUND THREAD YT: Terminato. Successo: {job_success}. Messaggio: {thread_final_message}")