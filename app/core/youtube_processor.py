import logging
import sqlite3
import threading
import textstat
import copy
from typing import Optional
from google.api_core import exceptions as google_exceptions

from flask import current_app

from app.services.youtube.client import YouTubeClient
from app.services.transcripts.youtube_transcript import TranscriptService
from app.services.embedding.embedding_service import generate_embeddings
from app.services.embedding.gemini_embedding import split_text_into_chunks, TASK_TYPE_DOCUMENT
from app.utils import build_full_config_for_background_process


logger = logging.getLogger(__name__)

def _process_youtube_channel_core(channel_id: str, user_id: Optional[str], core_config: dict) -> bool:
    """
    Logica centrale per processare TUTTI i video NUOVI di un canale.
    Usa il dizionario core_config per tutte le impostazioni e i client.
    """
    logger.info(f"[CORE YT Process] Avvio per channel_id={channel_id}, user_id={user_id}, config_keys={list(core_config.keys()) if core_config else 'None'}")
    overall_success = False
    conn_sqlite = None
    processed_videos_data_for_db = []
    yt_count, db_existing_count, to_process_count, saved_ok_count = 0, 0, 0, 0
    transcript_errors, embedding_errors, chroma_errors, generic_errors = 0, 0, 0, 0

    try:
        # --- 1. ESTRAI CONFIGURAZIONE E RISORSE DA core_config ---
        app_mode = core_config.get('APP_MODE')
        token_path = core_config.get('TOKEN_PATH')
        db_path_sqlite = core_config.get('DATABASE_FILE')
        llm_api_key = core_config.get('GOOGLE_API_KEY')
        embedding_model = core_config.get('GEMINI_EMBEDDING_MODEL')
        base_video_collection_name = core_config.get('VIDEO_COLLECTION_NAME')
        chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS')
        chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS')
        
        # Client e collezioni Chroma specifici presi da core_config
        # (Questi vengono preparati in create_app o nello scheduler_jobs)
        chroma_client_from_core_config = core_config.get('CHROMA_CLIENT')
        # La collezione singola è rilevante solo se app_mode è 'single'
        chroma_collection_single_from_core_config = core_config.get('CHROMA_VIDEO_COLLECTION') if app_mode == 'single' else None

        # --- Validazione Configurazione Essenziale ---
        required_config_keys = {
            'APP_MODE', 'TOKEN_PATH', 'DATABASE_FILE', 'GOOGLE_API_KEY', 
            'GEMINI_EMBEDDING_MODEL', 'VIDEO_COLLECTION_NAME', 
            'DEFAULT_CHUNK_SIZE_WORDS', 'DEFAULT_CHUNK_OVERLAP_WORDS',
            'CHROMA_CLIENT' # Il client è sempre necessario per ottenere/creare collezioni
        }
        missing_keys = [key for key in required_config_keys if core_config.get(key) is None]
        if missing_keys:
            logger.error(f"[CORE YT Process] Chiavi di configurazione mancanti in core_config: {missing_keys}")
            raise RuntimeError(f"Configurazione incompleta fornita a _process_youtube_channel_core: {', '.join(missing_keys)}")

        # Verifica specifica per Chroma in base alla modalità
        is_chroma_setup_ok = False
        if app_mode == 'single':
            is_chroma_setup_ok = chroma_collection_single_from_core_config is not None
            if not is_chroma_setup_ok: 
                logger.error("[CORE YT Process] Modalità SINGLE ma CHROMA_VIDEO_COLLECTION non fornita in core_config.")
        elif app_mode == 'saas':
            is_chroma_setup_ok = chroma_client_from_core_config is not None
            if not is_chroma_setup_ok:
                logger.error("[CORE YT Process] Modalità SAAS ma CHROMA_CLIENT non fornito in core_config.")
        
        if not is_chroma_setup_ok:
             raise RuntimeError("Setup ChromaDB incompleto in core_config per la modalità operativa corrente.")
        
        logger.info(f"[CORE YT Process] Configurazione letta da core_config: OK. APP_MODE='{app_mode}'")
        
        # --- Connessione SQLite ---
        logger.info(f"[CORE YT Process] Apertura connessione SQLite: {db_path_sqlite}")
        conn_sqlite = sqlite3.connect(db_path_sqlite, timeout=10.0)
        cursor_sqlite = conn_sqlite.cursor()
        logger.info("[CORE YT Process] Connessione SQLite aperta.")

        # --- 2. RECUPERA VIDEO DA YOUTUBE ---
        logger.info(f"[CORE YT Process] Recupero video da YouTube per canale {channel_id}...")
        youtube_client = YouTubeClient(token_file=token_path) # Usa token_path da core_config
        videos_from_yt_models = youtube_client.get_channel_videos(channel_id)
        yt_count = len(videos_from_yt_models)
        logger.info(f"[CORE YT Process] Recuperati {yt_count} video totali da YouTube.")

        # --- 3. RECUPERA ID ESISTENTI DA SQLITE ---
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

        # --- 4. IDENTIFICA VIDEO NUOVI ---
        videos_to_process_models = [v for v in videos_from_yt_models if v.video_id not in existing_video_ids]
        to_process_count = len(videos_to_process_models)
        if to_process_count == 0:
            logger.info("[CORE YT Process] Nessun nuovo video da processare."); overall_success = True
        else:
            logger.info(f"[CORE YT Process] Identificati {to_process_count} nuovi video da processare.")
            
            # --- OTTIENI COLLEZIONE CHROMA CORRETTA PER L'UPSERT ---
            chroma_collection_for_upsert = None
            collection_name_for_log = "N/A"
            if app_mode == 'single':
                chroma_collection_for_upsert = chroma_collection_single_from_core_config # Già un'istanza
                if chroma_collection_for_upsert: collection_name_for_log = chroma_collection_for_upsert.name
            elif app_mode == 'saas':
                user_video_collection_name = f"{base_video_collection_name}_{user_id}"
                collection_name_for_log = user_video_collection_name
                try:
                    # Usa il client_from_core_config per ottenere/creare la collezione
                    chroma_collection_for_upsert = chroma_client_from_core_config.get_or_create_collection(name=user_video_collection_name)
                    logger.info(f"[CORE YT Process] Collezione SAAS '{collection_name_for_log}' pronta per upsert.")
                except Exception as e_saas_coll_upsert:
                    logger.error(f"[CORE YT Process] Fallimento get/create collezione SAAS '{collection_name_for_log}' per upsert: {e_saas_coll_upsert}")
                    # chroma_collection_for_upsert rimane None

            if not chroma_collection_for_upsert:
                 logger.warning(f"[CORE YT Process] Collezione Chroma '{collection_name_for_log}' NON disponibile per l'upsert. L'embedding e l'upsert saranno saltati o falliranno.")
                 # Questo diventerà un errore per ogni video se si tenta l'upsert

            # --- 5. CICLO ELABORAZIONE VIDEO NUOVI ---
            for index, video_model in enumerate(videos_to_process_models, 1):
                video_id = video_model.video_id; video_title = video_model.title
                logger.info(f"[CORE YT Process] --- Processing video {index}/{to_process_count}: {video_id} ({video_title}) ---")
                current_video_status = 'pending'; transcript_text, transcript_lang, transcript_type = None, None, None; chunks = []
                try:
                    import time
                    transcript_result = None
                    last_transcript_error = None
                    # Prova fino a 3 volte con una pausa tra i tentativi
                    for attempt in range(3):
                        logger.info(f"[CORE YT Process] [{video_id}] Tentativo recupero trascrizione #{attempt + 1}...")
                        transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)
                        if transcript_result and not transcript_result.get('error'):
                            logger.info(f"[CORE YT Process] [{video_id}] Trascrizione ottenuta con successo al tentativo #{attempt + 1}.")
                            break # Successo, esci dal ciclo
                        else:
                            last_transcript_error = transcript_result.get('message', 'Errore sconosciuto') if transcript_result else 'Nessuna trascrizione trovata'
                            logger.warning(f"[CORE YT Process] [{video_id}] Tentativo #{attempt + 1} fallito: {last_transcript_error}. Attendo 2 secondi...")
                            time.sleep(2) # Pausa prima del prossimo tentativo
                    
                    if transcript_result and not transcript_result.get('error'):
                        transcript_text, transcript_lang, transcript_type = transcript_result['text'], transcript_result['language'], transcript_result['type']
                        current_video_status = 'processing_embedding'
                    else: 
                        current_video_status = 'failed_transcript'
                        transcript_errors += 1
                        logger.error(f"[CORE YT Process] [{video_id}] Recupero trascrizione fallito dopo 3 tentativi. Errore finale: {last_transcript_error}")
                    
                    if current_video_status == 'processing_embedding' and transcript_text:
                        chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                        if not chunks: current_video_status = 'completed' # No chunks, ma trascrizione OK
                    elif current_video_status != 'failed_transcript': # Se non è già fallita la trascrizione
                        current_video_status = 'failed_transcript'; transcript_errors += 1 # Segna errore se testo trascrizione non valido per chunking
                        logger.warning(f"[CORE YT Process] [{video_id}] Testo trascrizione mancante/invalido per chunking.")


                    if current_video_status == 'processing_embedding' and chunks:
                        if not chroma_collection_for_upsert: # Controlla di nuovo se la collezione è disponibile
                            logger.error(f"[CORE YT Process] [{video_id}] Impossibile procedere con embedding/upsert, collezione Chroma '{collection_name_for_log}' non disponibile.")
                            current_video_status = 'failed_chroma_config'; chroma_errors += 1
                        else:
                            try:
                                user_settings_for_embedding = {'llm_provider': 'google', 'llm_api_key': llm_api_key, 'llm_embedding_model': embedding_model}
                                embeddings = generate_embeddings(chunks, user_settings=user_settings_for_embedding, task_type=TASK_TYPE_DOCUMENT)
                                if embeddings and len(embeddings) == len(chunks):
                                    ids = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                                    metadatas_chroma = [{
                                        "video_id": video_id, "channel_id": video_model.channel_id, "video_title": video_model.title,
                                        "published_at": str(video_model.published_at), "chunk_index": i, "language": transcript_lang,
                                        "caption_type": transcript_type,
                                        **({"user_id": user_id} if app_mode == 'saas' and user_id else {})
                                    } for i in range(len(chunks))]
                                    chroma_collection_for_upsert.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas_chroma, documents=chunks)
                                    current_video_status = 'completed'
                                else: current_video_status = 'failed_embedding'; embedding_errors += 1
                            except google_exceptions.ResourceExhausted: current_video_status = 'failed_embedding_ratelimit'; embedding_errors += 1
                            except google_exceptions.GoogleAPIError: current_video_status = 'failed_embedding_api'; embedding_errors += 1
                            except Exception as e_emb_chroma: 
                                logger.exception(f"[CORE YT Process] [{video_id}] Errore Embedding o Chroma Upsert.")
                                current_video_status = 'failed_chroma_write' if 'upsert' in str(e_emb_chroma).lower() else 'failed_embedding'
                                if current_video_status == 'failed_chroma_write': chroma_errors +=1
                                else: embedding_errors += 1
                                
                except Exception as e_video_proc:
                    logger.exception(f"[CORE YT Process] Errore generico processo video {video_id}.")
                    current_video_status = 'failed_processing'; generic_errors += 1

                                # Aggiungiamo il calcolo delle statistiche PRIMA di preparare i dati per il DB
                if current_video_status == 'completed':
                    try:
                        stats = { 'word_count': 0, 'gunning_fog': 0 }
                        if transcript_text and transcript_text.strip():
                            stats['word_count'] = len(transcript_text.split())
                            stats['gunning_fog'] = textstat.gunning_fog(transcript_text)
                        
                        # Inserisce o aggiorna le statistiche nella tabella cache
                        cursor_sqlite.execute("""
                            INSERT INTO content_stats (content_id, user_id, source_type, word_count, gunning_fog)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(content_id) DO UPDATE SET
                                word_count = excluded.word_count,
                                gunning_fog = excluded.gunning_fog,
                                last_calculated = CURRENT_TIMESTAMP
                        """, (video_id, user_id, 'videos', stats['word_count'], stats['gunning_fog']))
                        logger.info(f"[CORE YT Process] [{video_id}] Statistiche salvate/aggiornate nella cache.")
                    except Exception as e_stats:
                        logger.error(f"[CORE YT Process] [{video_id}] Errore durante il calcolo/salvataggio delle statistiche: {e_stats}")
                        # Non cambiamo lo stato del video per questo, ma lo logghiamo
                
                video_data_dict = video_model.model_dump()
                video_data_dict.update({
                    'transcript': transcript_text, 'transcript_language': transcript_lang,
                    'captions_type': transcript_type, 'processing_status': current_video_status, 'user_id': user_id
                })
                processed_videos_data_for_db.append(video_data_dict)
                logger.info(f"[CORE YT Process] --- Fine video {video_id}. Status: {current_video_status} ---")

            # --- 6. SALVATAGGIO BATCH SQLITE ---
            if processed_videos_data_for_db:
                logger.info(f"[CORE YT Process] Salvataggio batch SQLite per {len(processed_videos_data_for_db)} records...")
                try:
                    sql_insert = ''' INSERT OR REPLACE INTO videos (
                                        video_id, title, url, channel_id, published_at, description,
                                        transcript, transcript_language, captions_type, user_id,
                                        processing_status, added_at )
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) '''
                    data_to_insert = [(
                        vd['video_id'], vd['title'], vd['url'], vd['channel_id'], str(vd['published_at']), 
                        vd['description'], vd['transcript'], vd['transcript_language'], vd['captions_type'], 
                        vd['user_id'], vd.get('processing_status', 'failed')
                    ) for vd in processed_videos_data_for_db]
                    cursor_sqlite.executemany(sql_insert, data_to_insert)
                    conn_sqlite.commit()
                    saved_ok_count = cursor_sqlite.rowcount if cursor_sqlite.rowcount != -1 else len(data_to_insert)
                    logger.info(f"[CORE YT Process] Salvataggio batch SQLite OK ({saved_ok_count} righe).")
                    overall_success = True # Consideriamo successo generale se il batch DB va a buon fine
                except sqlite3.Error as e_sql_batch:
                    logger.error(f"[CORE YT Process] ERRORE BATCH SQLITE: {e_sql_batch}", exc_info=True)
                    if conn_sqlite: conn_sqlite.rollback()
                    overall_success = False # Errore critico nel salvataggio DB
    
    except RuntimeError as rte: 
         logger.error(f"[CORE YT Process] Errore runtime (config?): {rte}")
         overall_success = False
    except sqlite3.Error as e_sql_outer:
        logger.error(f"[CORE YT Process] Errore SQLite esterno: {e_sql_outer}", exc_info=True)
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    except Exception as e_core_generic:
        logger.exception(f"[CORE YT Process] Errore generico imprevisto in _process_youtube_channel_core.")
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    finally:
        if conn_sqlite:
            try: conn_sqlite.close(); logger.info("[CORE YT Process] Connessione SQLite chiusa.")
            except: pass 
        log_summary = (f"[CORE YT Process] Riepilogo per canale {channel_id} (Utente: {user_id}): "
                       f"YT Totali:{yt_count}, DB Esistenti:{db_existing_count}, Tentati:{to_process_count}, "
                       f"Salvati DB:{saved_ok_count}. Errori-> T:{transcript_errors}, E:{embedding_errors}, "
                       f"C:{chroma_errors}, G:{generic_errors}. Successo Generale: {overall_success}")
        if overall_success: logger.info(log_summary)
        else: logger.error(log_summary)
    return {
        "success": overall_success,
        "new_videos_processed": saved_ok_count,
        "total_videos_on_yt": yt_count
    }


def _background_channel_processing(app_context, channel_url: str, user_id: Optional[str], initial_status: dict):
    """
    Esegue l'elaborazione del canale in un thread separato, chiamando la funzione core.
    Aggiorna lo stato globale 'processing_status' per la UI.
    """
    global processing_status # Riferimento globale per stato UI
    status_lock = threading.Lock() # Lock per aggiornamenti sicuri dello stato UI
    thread_final_message = "Elaborazione background terminata con stato sconosciuto."
    job_success = False # Successo dell'esecuzione core

    # Imposta stato iniziale UI
    with status_lock:
        processing_status.update(initial_status)
        processing_status['is_processing'] = True # Assicura sia True all'inizio
        processing_status['message'] = 'Avvio elaborazione canale...'
    logger.info(f"BACKGROUND THREAD YT: Avvio per channel_url={channel_url}, user_id={user_id}")

    channel_id_extracted = None # Memorizza l'ID estratto

    with app_context: # Esegui nel contesto app per accedere a config, etc.
        try:
            from app.main import load_credentials
            if not load_credentials():
                logger.error("BACKGROUND THREAD YT: Credenziali non trovate. Processo interrotto.")
                with status_lock:
                    processing_status['is_processing'] = False
                    # Questo messaggio apparirà nel banner di stato
                    processing_status['message'] = "Errore: Autorizzazione Google richiesta. Vai alla home e accedi con Google per riattivarla."
                return # Interrompe la funzione in modo pulito
            # --- 1. Estrai Channel ID ---
            # È meglio farlo qui piuttosto che nella funzione core,
            # perché l'URL potrebbe essere fornito in vari formati.
            token_path = current_app.config.get('TOKEN_PATH')
            if not token_path: # Controllo importante
                logger.error("[CORE YT Process] Token path mancante nella configurazione!")
                raise RuntimeError("Token path mancante per YouTubeClient nel core process.")
            youtube_client = YouTubeClient(token_file=token_path)
            logger.info(f"BACKGROUND THREAD YT: Estraggo channel ID da {channel_url}...")
            channel_id_extracted = youtube_client.extract_channel_info(channel_url)
            if not channel_id_extracted:
                 raise ValueError(f"Impossibile estrarre un Channel ID valido da '{channel_url}'.")
            logger.info(f"BACKGROUND THREAD YT: Channel ID estratto: {channel_id_extracted}")

            # === COSTRUISCI IL DIZIONARIO core_config QUI ===
            app_mode_value = current_app.config.get('APP_MODE', 'single') # Prendi il valore una volta
            core_config_dict = build_full_config_for_background_process(user_id)
            # La validazione required_keys_yt in _process_youtube_channel_core verificherà che 'APP_MODE' sia presente.
            # Il controllo `missing_keys` qui sotto è ridondante se _process_youtube_channel_core lo fa già, ma non fa male.
            required_keys_yt_for_background = ['APP_MODE', 'TOKEN_PATH', 'DATABASE_FILE', 'GOOGLE_API_KEY', 'CHROMA_CLIENT'] 
            missing_keys = [k for k in required_keys_yt_for_background if not core_config_dict.get(k)]
            if missing_keys: 
                raise RuntimeError(f"Valori mancanti nella config per il thread YT: {', '.join(missing_keys)}")
                logger.info(f"BACKGROUND THREAD YT: Dizionario 'core_config_dict' preparato per canale {channel_id_extracted} con chiavi: {list(core_config_dict.keys())}")
            
            # 2. Chiama la Funzione Core con tre argomenti
            result_data = _process_youtube_channel_core(
                channel_id_extracted,
                user_id,
                core_config_dict
            )
            job_success = result_data.get("success", False)
            new_videos_count = result_data.get("new_videos_processed", 0)

            if job_success:
                if new_videos_count > 0:
                    thread_final_message = f"Processo completato! Aggiunti {new_videos_count} nuovi video."
                else:
                    thread_final_message = "Canale già aggiornato. Nessun nuovo video trovato."
            else:
                 thread_final_message = f"Si sono verificati errori durante il processo del canale. Controllare i log del server."

        except Exception as e_thread:
            logger.exception(f"BACKGROUND THREAD YT: ERRORE CRITICO.")
            thread_final_message = f"Errore critico elaborazione canale: {e_thread}"
            job_success = False # Assicura sia False
        finally:
            # --- AGGIORNA STATO GLOBALE UI FINALE ---
            logger.info(f"BACKGROUND THREAD YT: Esecuzione finally.")
            with status_lock:
                processing_status['is_processing'] = False
                processing_status['current_video'] = None
                # Potremmo voler distinguere errore da successo nel messaggio UI
                processing_status['message'] = thread_final_message
                # Non serve un campo 'error' qui, il messaggio lo contiene
            logger.info(f"BACKGROUND THREAD YT: Terminato. Successo Core Job: {job_success}. Messaggio UI: {thread_final_message}")
