import io
import logging
import sqlite3
import chromadb
from datetime import datetime
from flask import Blueprint, jsonify, request, current_app, Response
from flask_login import login_required, current_user
from google.api_core import exceptions as google_exceptions
from typing import Optional
import threading
import textstat
import copy


# --- Import Servizi e Moduli App ---
from app.services.youtube.client import YouTubeClient
from app.services.transcripts.youtube_transcript import TranscriptService
# Assicurati che l'import del modulo embedding sia corretto
from app.services.embedding.gemini_embedding import split_text_into_chunks, get_gemini_embeddings, TASK_TYPE_DOCUMENT

# --- Setup Logger e Blueprint ---
logger = logging.getLogger(__name__)
videos_bp = Blueprint('videos', __name__)

# --- Variabile Stato Globale (Invariata) ---
processing_status = {
    'current_video': None, 'total_videos': 0,
    'is_processing': False, 'message': ''
}

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
                                embeddings = get_gemini_embeddings(chunks, api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_DOCUMENT)
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
                
                video_data_dict = video_model.dict()
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
            core_config_dict = {
                'APP_MODE': app_mode_value, # <--- AGGIUNGI QUESTA RIGA
                'TOKEN_PATH': token_path, 
                'DATABASE_FILE': current_app.config.get('DATABASE_FILE'),
                'GOOGLE_API_KEY': current_app.config.get('GOOGLE_API_KEY'),
                'GEMINI_EMBEDDING_MODEL': current_app.config.get('GEMINI_EMBEDDING_MODEL'),
                'VIDEO_COLLECTION_NAME': current_app.config.get('VIDEO_COLLECTION_NAME'),
                'DEFAULT_CHUNK_SIZE_WORDS': current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS'),
                'DEFAULT_CHUNK_OVERLAP_WORDS': current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS'),
                'CHROMA_CLIENT': current_app.config.get('CHROMA_CLIENT'),
                # Passa CHROMA_VIDEO_COLLECTION solo se rilevante per la modalità attuale
                'CHROMA_VIDEO_COLLECTION': current_app.config.get('CHROMA_VIDEO_COLLECTION') if app_mode_value == 'single' else None,
            }
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

# --- Route Processa Canale ---
@videos_bp.route('/channel', methods=['POST'])
@login_required
def process_channel():
    """
    AVVIA l'elaborazione di un canale YouTube in un thread separato
    e restituisce subito una risposta.
    """
    global processing_status
    # Usa un lock anche qui per sicurezza nell'accesso iniziale allo stato
    status_lock = threading.Lock()

    logger.info("Richiesta ricevuta per processare canale (avvio thread).")
    app_mode = current_app.config.get('APP_MODE', 'single')

    # --- OTTIENI USER ID REALE ---
    current_user_id = None
    if app_mode == 'saas':
        if not current_user.is_authenticated: return jsonify({'success': False, 'error_code': 'AUTH_REQUIRED'}), 401
        current_user_id = current_user.id
        logger.info(f"Richiesta processo canale per utente '{current_user_id}'")
    else:
         logger.info("Richiesta processo canale in modalità SINGLE.")

    # --- CONTROLLO SE GIÀ IN ELABORAZIONE ---
    with status_lock:
        if processing_status.get('is_processing', False):
            logger.warning("Tentativo di avviare elaborazione canale mentre un'altra è già in corso.")
            return jsonify({'success': False, 'error_code': 'ALREADY_PROCESSING', 'message': 'Un processo di elaborazione canale è già attivo. Attendi il completamento.'}), 409 # Conflict

    # --- LEGGI E VALIDA INPUT JSON ---
    if not request.is_json:
         # ... (return errore 400) ...
         return jsonify({'success': False, 'error_code': 'INVALID_CONTENT_TYPE', 'message': 'Request must be JSON.'}), 400
    data = request.get_json()
    channel_url = data.get('channel_url')
    if not channel_url or not isinstance(channel_url, str) or not channel_url.strip():
        # ... (return errore 400) ...
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': 'Channel URL mancante o non valido.'}), 400
    logger.info(f"Input channel_url valido: {channel_url}")

    # --- IMPOSTA STATO INIZIALE e AVVIA THREAD ---
    try:
        # Prepara stato iniziale da passare al thread
        initial_status_for_thread = {
            'is_processing': True,
            'current_video': None,
            'total_videos': 0, # Verrà aggiornato dal thread
            'message': 'Avvio elaborazione in background...'
        }

        # Ottieni il contesto dell'app corrente per passarlo al thread
        # È NECESSARIO per accedere a current_app.config dal thread
        app_context = current_app.app_context()

        # Crea e avvia il thread
        background_thread = threading.Thread(
            target=_background_channel_processing,
            args=(app_context, channel_url, current_user_id, copy.deepcopy(initial_status_for_thread)) # Passa una copia dello stato
        )
        background_thread.daemon = True # Permette all'app di uscire anche se il thread è attivo (opzionale)
        background_thread.start()
        logger.info(f"Thread in background avviato per processare canale: {channel_url}")

        # Aggiorna lo stato globale *dopo* aver avviato il thread con successo
        # (il thread lo sovrascriverà comunque, ma impostiamolo anche qui)
        with status_lock:
            processing_status.update(initial_status_for_thread)

        # --- RESTITUISCI RISPOSTA IMMEDIATA ---
        return jsonify({
            'success': True,
            'message': 'Elaborazione canale avviata in background. Controlla lo stato periodicamente.'
        }), 202 # Accepted: la richiesta è stata accettata ma l'elaborazione non è completa

    except Exception as e_start:
        logger.exception("Errore CRITICO durante l'avvio del thread di elaborazione.")
        # Resetta lo stato globale se l'avvio fallisce
        with status_lock:
            processing_status['is_processing'] = False
            processing_status['message'] = f"Errore avvio elaborazione: {e_start}"
        return jsonify({'success': False, 'error_code': 'THREAD_START_FAILED', 'message': f'Errore avvio processo background: {e_start}'}), 500

# --- Route Riprocessa Singolo Video ---
@videos_bp.route('/<string:video_id>/reprocess', methods=['POST'])
@login_required
def reprocess_single_video(video_id):
    """
    Forza il riprocessamento completo di un singolo video (verifica utente,
    trascrizione, chunking, embedding, pulizia ChromaDB, upsert ChromaDB, update SQLite).
    """
    with current_app.app_context():
        from app.main import load_credentials
        credentials = load_credentials()
        if not credentials or not credentials.valid:
            logger.warning(f"Tentativo di riprocessare {video_id} senza credenziali valide.")
            # Restituiamo un JSON di errore specifico che JavaScript può interpretare
            return jsonify({
                'success': False,
                'error_code': 'REAUTH_REQUIRED',
                'message': 'Autorizzazione Google richiesta.',
                'redirect_url': '/authorize' # Scriviamo l'URL direttamente, più robusto in contesti API
                }), 401 # 401 Unauthorized è il codice HTTP corretto
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[Reprocess Single] Richiesta per video ID: {video_id} (Modalità: {app_mode})")

    # --- Ottieni User ID ---
    current_user_id = None
    if app_mode == 'saas':
        if not current_user.is_authenticated: return jsonify({'success': False, 'error_code': 'AUTH_REQUIRED'}), 401
        current_user_id = current_user.id
        logger.info(f"[Reprocess Single] [{video_id}] Riprocessamento per utente '{current_user_id}'")

    # --- Variabili Locali ---
    final_status = 'pending'
    message = f"Inizio riprocessamento {video_id}"
    transcript_text, transcript_lang, transcript_type = None, None, None
    conn_sqlite = None
    chroma_collection_to_use = None
    collection_name_for_log = "N/A"
    update_db_success = False # Flag per tracciare successo scrittura finale DB

    try:
        # --- 1. Setup Configurazione ---
        logger.debug(f"[Reprocess Single] [{video_id}] Verifica config...")
        db_path_sqlite = current_app.config.get('DATABASE_FILE')
        llm_api_key = current_app.config.get('GOOGLE_API_KEY')
        embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
        chunk_size = current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
        chunk_overlap = current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
        base_video_collection_name = current_app.config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
        chroma_client = current_app.config.get('CHROMA_CLIENT') # Serve sempre per ottenere collezione
        chroma_collection_single = current_app.config.get('CHROMA_VIDEO_COLLECTION') # Solo per single

        if not all([db_path_sqlite, llm_api_key, embedding_model, base_video_collection_name, chroma_client]): # Client serve sempre
            raise RuntimeError("Configurazione server incompleta (DB, API Key, Embedding, Chroma Client).")
        if app_mode == 'single' and not chroma_collection_single:
             raise RuntimeError("Configurazione server incompleta (Collezione Chroma Single Mode).")
        logger.debug(f"[Reprocess Single] [{video_id}] Config OK.")


        # --- 2. Connessione DB e Recupero Metadati Video ---
        logger.debug(f"[Reprocess Single] [{video_id}] Connessione DB: {db_path_sqlite}")
        conn_sqlite = sqlite3.connect(db_path_sqlite)
        conn_sqlite.row_factory = sqlite3.Row
        cursor_sqlite = conn_sqlite.cursor()

        sql_get_meta = "SELECT video_id, title, channel_id, published_at, description FROM videos WHERE video_id = ?"
        params_get_meta = [video_id]
        if app_mode == 'saas':
            sql_get_meta += " AND user_id = ?"
            params_get_meta.append(current_user_id)
            logger.info(f"[Reprocess Single] [{video_id}] SAAS: Verifico appartenenza utente '{current_user_id}'...")

        cursor_sqlite.execute(sql_get_meta, tuple(params_get_meta))
        video_meta = cursor_sqlite.fetchone()

        if not video_meta:
            message = f"Video ID '{video_id}' non trovato" + (f" per utente '{current_user_id}'" if app_mode=='saas' else "") + "."
            logger.warning(f"[Reprocess Single] {message}")
            if conn_sqlite: conn_sqlite.close()
            return jsonify({'success': False, 'error_code': 'VIDEO_NOT_FOUND', 'message': message}), 404
        logger.info(f"[Reprocess Single] [{video_id}] Metadati recuperati: {video_meta['title']}")
        # Converti in dizionario per comodità
        video_meta_dict = dict(video_meta)

        # --- 3. Aggiorna Stato Iniziale (Opzionale ma consigliato) ---
        # cursor_sqlite.execute("UPDATE videos SET processing_status = 'processing_reprocess' WHERE video_id = ?", (video_id,))
        # logger.debug(f"[Reprocess Single] [{video_id}] Stato DB impostato a 'processing_reprocess'.")
        # Non facciamo commit qui ancora

        # --- 4. Recupero Trascrizione ---
        logger.info(f"[Reprocess Single] [{video_id}] Recupero trascrizione...")
        # Creiamo un'istanza del client usando il token dalla configurazione
        token_path = current_app.config.get('TOKEN_PATH')
        youtube_client = YouTubeClient(token_file=token_path)

        # Ora chiamiamo get_transcript passando il client
        transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)
        if transcript_result and not transcript_result.get('error'):
            # Successo, abbiamo la trascrizione
            transcript_text, transcript_lang, transcript_type = transcript_result['text'], transcript_result['language'], transcript_result['type']
            final_status = 'processing_embedding'
            logger.info(f"[Reprocess Single] [{video_id}] Trascrizione OK.")
        else:
            # Fallimento, usiamo il messaggio di errore specifico
            final_status = 'failed_transcript'
            error_details = transcript_result.get('message', 'Trascrizione non trovata o disabilitata.') if transcript_result else 'Trascrizione non trovata o disabilitata.'
            message = f"Riprocessamento {video_id} fallito: {error_details}" # Aggiorniamo il messaggio per l'utente
            logger.warning(f"[Reprocess Single] [{video_id}] Trascrizione fallita. Motivo: {error_details}")
                # --- 5. Chunking, Embedding, Chroma ---
        chunks = [] # Inizializza chunks qui
        if final_status == 'processing_embedding' and transcript_text:
            try:
                logger.debug(f"[Reprocess Single] [{video_id}] Chunking...")
                chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

                if chunks:
                    logger.info(f"[Reprocess Single] [{video_id}] {len(chunks)} chunks creati.")
                    logger.debug(f"[Reprocess Single] [{video_id}] Embedding...")
                    embeddings = get_gemini_embeddings(chunks, api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_DOCUMENT)

                    if embeddings and len(embeddings) == len(chunks):
                        logger.info(f"[Reprocess Single] [{video_id}] Embedding OK.")

                        # --- 5.a Ottieni Collezione Chroma ---
                        logger.debug(f"[Reprocess Single] [{video_id}] Ottenimento collezione Chroma...")
                        if app_mode == 'single':
                             chroma_collection_to_use = chroma_collection_single
                             if chroma_collection_to_use: collection_name_for_log = chroma_collection_to_use.name
                        elif app_mode == 'saas':
                             user_video_collection_name = f"{base_video_collection_name}_{current_user_id}"
                             collection_name_for_log = user_video_collection_name
                             try: chroma_collection_to_use = chroma_client.get_or_create_collection(name=user_video_collection_name)
                             except Exception as e_saas_coll: logger.error(f"[Reprocess Single] [{video_id}] Fallimento get/create coll. SAAS '{collection_name_for_log}': {e_saas_coll}"); chroma_collection_to_use = None
                        if not chroma_collection_to_use:
                            logger.error(f"[Reprocess Single] [{video_id}] Collezione Chroma '{collection_name_for_log}' NON disponibile!")
                            final_status = 'failed_chroma_config' # Nuovo stato errore
                        else:
                             logger.info(f"[Reprocess Single] [{video_id}] Uso collezione Chroma: '{collection_name_for_log}'")

                             # --- 5.b Elimina Vecchi Chunk da Chroma ---
                             if final_status == 'processing_embedding': # Procedi solo se non ci sono stati errori prima
                                 try:
                                     logger.info(f"[Reprocess Single] [{video_id}] Tentativo eliminazione vecchi chunk da Chroma '{collection_name_for_log}'...")
                                     existing_chunks = chroma_collection_to_use.get(where={"video_id": video_id}, include=[]) # Solo ID
                                     ids_to_delete = existing_chunks.get('ids', [])
                                     if ids_to_delete:
                                         logger.debug(f"[Reprocess Single] [{video_id}] Elimino {len(ids_to_delete)} chunk IDs: {ids_to_delete}")
                                         chroma_collection_to_use.delete(ids=ids_to_delete)
                                         logger.info(f"[Reprocess Single] [{video_id}] Vecchi chunk eliminati da Chroma.")
                                     else:
                                         logger.info(f"[Reprocess Single] [{video_id}] Nessun vecchio chunk da eliminare trovato in Chroma.")
                                 except Exception as e_chroma_del:
                                     # Logga errore ma continua per tentare l'upsert (potrebbe essere il primo processamento)
                                     logger.error(f"[Reprocess Single] [{video_id}] Errore durante eliminazione vecchi chunk Chroma: {e_chroma_del}", exc_info=True)
                                     # Non cambiamo lo stato qui, l'upsert fallirà se la collezione ha problemi seri

                             # --- 5.c Upsert Nuovi Chunk in Chroma ---
                             if final_status == 'processing_embedding': # Controlla di nuovo
                                 try:
                                     ids_upsert = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                                     metadatas_upsert = [{
                                         'video_id': video_id, 'channel_id': video_meta_dict['channel_id'], 'video_title': video_meta_dict['title'],
                                         'published_at': str(video_meta_dict['published_at']), 'chunk_index': i, 'language': transcript_lang,
                                         'caption_type': transcript_type,
                                         **({"user_id": current_user_id} if app_mode == 'saas' else {})
                                     } for i in range(len(chunks))]
                                     logger.info(f"[Reprocess Single] [{video_id}] Upsert di {len(chunks)} nuovi chunk in Chroma '{collection_name_for_log}'...")
                                     chroma_collection_to_use.upsert(ids=ids_upsert, embeddings=embeddings, metadatas=metadatas_upsert, documents=chunks)
                                     logger.info(f"[Reprocess Single] [{video_id}] Upsert Chroma OK.")
                                     final_status = 'completed' # Successo finale!
                                 except Exception as chroma_e_upsert:
                                     logger.exception(f"[Reprocess Single] [{video_id}] Errore Upsert Chroma.");
                                     final_status = 'failed_chroma_write'
                    # Gestione errori embedding
                    elif embeddings is None: final_status = 'failed_embedding'; logger.error(f"[Reprocess Single] [{video_id}] Embedding fallito (API/Config error).")
                    else: final_status = 'failed_embedding'; logger.error(f"[Reprocess Single] [{video_id}] Discrepanza Embedding/Chunks.")
                else: # Nessun chunk generato
                    final_status = 'completed'; logger.info(f"[Reprocess Single] [{video_id}] No chunks, marco come completo (senza upsert).")
                    # Se non ci sono chunk, dobbiamo comunque eliminare quelli vecchi da Chroma!
                    if chroma_collection_to_use:
                         try:
                             logger.info(f"[Reprocess Single] [{video_id}] Nessun nuovo chunk, elimino eventuali vecchi da Chroma...")
                             existing_chunks = chroma_collection_to_use.get(where={"video_id": video_id}, include=[])
                             ids_to_delete = existing_chunks.get('ids', [])
                             if ids_to_delete: chroma_collection_to_use.delete(ids=ids_to_delete); logger.info(f"[Reprocess Single] [{video_id}] Vecchi chunk eliminati (perché non ce ne sono di nuovi).")
                         except Exception as e_chroma_del_nochunks: logger.error(f"[Reprocess Single] [{video_id}] Errore eliminazione vecchi chunk (caso no-nuovi-chunk): {e_chroma_del_nochunks}")

            # Gestione eccezioni specifiche embedding/chunking
            except google_exceptions.ResourceExhausted: final_status = 'failed_embedding_ratelimit'; logger.error(f"[{video_id}] Rate limit embedding.")
            except google_exceptions.GoogleAPIError: final_status = 'failed_embedding_api'; logger.error(f"[{video_id}] API Error embedding.")
            except Exception as chunk_embed_e: final_status = 'failed_processing'; logger.exception(f"[{video_id}] Errore Chunk/Embed/Chroma prep.")
        # Altri casi (es. trascrizione fallita) mantengono lo stato già impostato

        # --- 6. Aggiornamento Finale SQLite ---
        logger.info(f"[Reprocess Single] [{video_id}] Aggiornamento finale DB. Stato: {final_status}, Lingua: {transcript_lang}, Tipo: {transcript_type}")
        cursor_sqlite.execute( """
            UPDATE videos
            SET transcript = ?, transcript_language = ?, captions_type = ?, processing_status = ?, added_at = CURRENT_TIMESTAMP
            WHERE video_id = ?
            """,
            (transcript_text, transcript_lang, transcript_type, final_status, video_id)
        )
        conn_sqlite.commit() # Commit di TUTTE le modifiche DB
        update_db_success = True
        logger.info(f"[Reprocess Single] [{video_id}] Aggiornamento finale DB OK.")

    # --- Gestione Eccezioni Generali della Route ---
    except sqlite3.Error as db_err_outer:
         logger.error(f"[Reprocess Single] [{video_id}] Errore DB esterno: {db_err_outer}. Stato probabile: {final_status}", exc_info=True)
         if conn_sqlite: conn_sqlite.rollback()
         message = f"Errore DB durante riprocessamento: {db_err_outer}"
         final_status = 'failed_db_error' # Stato generico DB
    except RuntimeError as conf_err:
        logger.error(f"[Reprocess Single] [{video_id}] Errore config: {conf_err}")
        if conn_sqlite: conn_sqlite.rollback()
        message = f"Errore configurazione: {conf_err}"
        final_status = 'failed_config_error'
    except Exception as e:
        logger.exception(f"[Reprocess Single] [{video_id}] Errore generico imprevisto: {e}")
        if conn_sqlite: conn_sqlite.rollback()
        message = f"Errore server imprevisto: {str(e)}"
        if final_status == 'pending' or final_status == 'processing_embedding': # Se l'errore è avvenuto prima di un fallimento specifico
             final_status = 'failed_unexpected'
    finally:
        if conn_sqlite:
             try: conn_sqlite.close(); logger.debug(f"[Reprocess Single] [{video_id}] Chiusura connessione DB nel finally.")
             except Exception as close_err: logger.warning(f"[Reprocess Single] [{video_id}] Errore chiusura connessione DB: {close_err}")

    # --- Risposta Finale ---
    is_overall_success = (final_status == 'completed')
    response_data = {
        'success': is_overall_success,
        'message': message,
        'new_status': final_status
    }
    status_code = 200 # Restituisci sempre 200 se l'API ha gestito l'operazione (anche se fallita)
                     # Tranne per errori 4xx (es. Not Found) gestiti prima

    # Aggiungi codice errore specifico se non successo
    if not is_overall_success:
         error_codes_map = {
             'failed_transcript': 'TRANSCRIPT_FAILED', 'failed_embedding': 'EMBEDDING_FAILED',
             'failed_embedding_ratelimit': 'EMBEDDING_RATE_LIMIT', 'failed_embedding_api': 'EMBEDDING_API_ERROR',
             'failed_chroma_write': 'VECTORDB_WRITE_FAILED', 'failed_processing': 'PROCESSING_ERROR',
             'failed_db_error': 'DB_OPERATION_ERROR', 'failed_config_error': 'SERVER_CONFIG_ERROR',
             'failed_unexpected': 'UNEXPECTED_SERVER_ERROR', 'failed_chroma_config': 'VECTORDB_CONFIG_ERROR'
         }
         response_data['error_code'] = error_codes_map.get(final_status, 'UNKNOWN_PROCESSING_FAILURE')

    logger.info(f"[Reprocess Single] [{video_id}] Risposta API: {response_data}")
    return jsonify(response_data), status_code

# --- Route Get Progress  ---
@videos_bp.route('/progress', methods=['GET'])
@login_required
def get_progress():
    return jsonify(processing_status)

@videos_bp.route('/all', methods=['DELETE'])
@login_required
def delete_all_user_videos():
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"Richiesta DELETE /all ricevuta (Modalità: {app_mode})")

    if app_mode != 'saas':
        # ... (return errore modalità non valida) ...
        return jsonify({'success': False, 'error_code': 'INVALID_MODE', 'message': 'Questa operazione è permessa solo in modalità SAAS.'}), 403

    current_user_id = current_user.id
    logger.info(f"Avvio eliminazione di massa video per utente: {current_user_id}")

    db_path = current_app.config.get('DATABASE_FILE')
    base_video_collection_name = current_app.config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
    chroma_client = current_app.config.get('CHROMA_CLIENT')

    if not db_path or not base_video_collection_name or not chroma_client:
        # ... (return errore config) ...
         return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Errore configurazione server.'}), 500

    user_video_collection_name = f"{base_video_collection_name}_{current_user_id}"
    conn_sqlite = None
    rows_affected = 0
    rows_after_delete = -1 # Valore iniziale per verifica

    try:
        # --- 1. Elimina da SQLite con Commit Immediato e Verifica ---
        logger.info(f"[{current_user_id}] Connessione a SQLite per eliminazione...")
        conn_sqlite = sqlite3.connect(db_path)
        cursor_sqlite = conn_sqlite.cursor()
        logger.info(f"[{current_user_id}] ESECUZIONE DELETE FROM videos WHERE user_id = ?...")
        cursor_sqlite.execute("DELETE FROM videos WHERE user_id = ?", (current_user_id,))
        rows_affected = cursor_sqlite.rowcount
        logger.info(f"[{current_user_id}] Righe potenzialmente affette dal DELETE: {rows_affected}. Tentativo COMMIT...")

        # Commit immediato
        conn_sqlite.commit()
        logger.info(f"[{current_user_id}] COMMIT SQLite ESEGUITO.")

        # Verifica post-commit (opzionale ma utile per debug)
        try:
            cursor_sqlite.execute("SELECT COUNT(*) FROM videos WHERE user_id = ?", (current_user_id,))
            count_result = cursor_sqlite.fetchone()
            rows_after_delete = count_result[0] if count_result else -1
            logger.info(f"[{current_user_id}] VERIFICA POST-COMMIT: Righe rimanenti per l'utente: {rows_after_delete}")
            if rows_after_delete != 0:
                 logger.error(f"[{current_user_id}] !!! ATTENZIONE: Il DELETE sembra non aver rimosso tutte le righe ({rows_after_delete} rimaste) nonostante il commit!!!")
                 # Potremmo decidere di restituire un errore qui se la verifica fallisce
                 # return jsonify({'success': False, 'error_code': 'DB_DELETE_VERIFY_FAILED', 'message': f'Verifica post-commit fallita: {rows_after_delete} righe video rimaste.'}), 500
        except Exception as e_verify:
            logger.error(f"[{current_user_id}] Errore durante la verifica post-commit SQLite: {e_verify}")
            # Continua comunque, ma segnala il problema

        # --- 2. Tenta di Eliminare Collezione ChromaDB (Logica invariata) ---
        logger.info(f"[{current_user_id}] Tentativo eliminazione collezione ChromaDB: '{user_video_collection_name}'...")
        try:
            chroma_client.delete_collection(name=user_video_collection_name)
            logger.info(f"[{current_user_id}] Comando delete_collection per '{user_video_collection_name}' inviato a ChromaDB.")
        except Exception as e_chroma:
            logger.error(f"[{current_user_id}] Errore durante il tentativo di eliminazione della collezione ChromaDB '{user_video_collection_name}': {e_chroma}", exc_info=True)

        # --- 3. Risposta ---
        final_message = f"Eliminazione video per utente {current_user_id} completata. Record SQLite affetti inizialmente: {rows_affected}."
        if rows_after_delete == 0:
            final_message += " Verifica post-commit SQLite OK (0 righe rimaste)."
        elif rows_after_delete > 0:
            final_message += f" ATTENZIONE: Verifica post-commit SQLite fallita ({rows_after_delete} righe rimaste)."
        final_message += " Tentativo pulizia ChromaDB inviato."

        return jsonify({
            'success': (rows_after_delete == 0), # Successo solo se la verifica post-commit è 0
            'message': final_message,
            'sqlite_rows_deleted_initial': rows_affected,
            'sqlite_rows_after_verify': rows_after_delete,
            'chroma_collection_to_delete': user_video_collection_name
            }), 200 if (rows_after_delete == 0) else 500 # Restituisci 500 se la verifica fallisce

    except sqlite3.Error as e_sql:
        # ... (gestione errori SQLite come prima) ...
        logger.error(f"[{current_user_id}] Errore SQLite durante eliminazione di massa: {e_sql}", exc_info=True)
        if conn_sqlite: conn_sqlite.rollback() # Rollback in caso di errore prima del commit
        return jsonify({'success': False, 'error_code': 'DB_OPERATION_ERROR', 'message': f'Errore database durante eliminazione di massa: {e_sql}'}), 500
    except Exception as e_outer:
         # ... (gestione errori generici come prima) ...
         logger.error(f"[{current_user_id}] Errore generico imprevisto durante eliminazione di massa: {e_outer}", exc_info=True)
         if conn_sqlite: conn_sqlite.rollback()
         return jsonify({'success': False, 'error_code': 'UNEXPECTED_DELETE_ERROR', 'message': f'Errore server imprevisto durante eliminazione di massa: {e_outer}'}), 500
    finally:
        if conn_sqlite:
            conn_sqlite.close()
            logger.debug(f"[{current_user_id}] Connessione SQLite chiusa per delete_all_user_videos.")

# --- Route Get Channel Info (Standardizzare errori?) ---
@videos_bp.route('/channel/info', methods=['POST'])
def get_channel_info():
    channel_url = request.json.get('channel_url')
    if not channel_url:
        # Errore 400 standardizzato
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': 'Channel URL is required.'}), 400
    try:
        token_path = current_app.config.get('TOKEN_PATH')
        if not token_path:
             logger.error("Token path mancante in config per /channel/info")
             raise RuntimeError("Server config error: Token path missing.") # Errore interno

        logger.info(f"Init YouTubeClient per /channel/info, token: {token_path}")
        youtube_client = YouTubeClient(token_file=token_path)
        channel_id = youtube_client.extract_channel_info(channel_url)
        videos = youtube_client.get_channel_videos(channel_id) # Restituisce lista di modelli Video
        # Converti modelli Pydantic in dizionari per JSON
        videos_dict = [v.dict() for v in videos]
        return jsonify({'success': True, 'channel_id': channel_id, 'videos': videos_dict })

    except ValueError as e_val: # Es. URL non valido, token invalido
         logger.error(f"Errore /channel/info (ValueError): {e_val}")
         # Potrebbe essere errore client (URL) o server (token)
         err_code = 'YOUTUBE_SETUP_ERROR' # O 'VALIDATION_ERROR' se l'URL è la causa più probabile?
         return jsonify({'success': False, 'error_code': err_code, 'message': str(e_val)}), 400 # Bad Request
    except RuntimeError as e_rt: # Errore config (token path)
        logger.error(f"Errore /channel/info (RuntimeError): {e_rt}")
        return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': str(e_rt)}), 500
    except Exception as e: # Altri errori API YouTube o imprevisti
        logger.exception(f"Errore imprevisto in /channel/info: {e}")
        return jsonify({'success': False, 'error_code': 'YOUTUBE_API_ERROR', 'message': f'Failed to get channel info: {str(e)}'}), 500


# --- Route Process Single Video Transcript (Standardizzare errori?) ---
@videos_bp.route('/process', methods=['POST'])
def process_video():
    video_id = request.json.get('video_id')
    if not video_id:
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': 'Video ID is required.'}), 400
    try:
        # has_manual_captions = TranscriptService.check_captions_availability(video_id) # Non necessario qui?
        # if not has_manual_captions:
        #     return jsonify({'success': False, 'error_code': 'NO_MANUAL_CAPTIONS', 'message': 'No manual captions available for this video.'}), 404

        transcript_result = TranscriptService.get_transcript(video_id)
        if transcript_result and not transcript_result.get('error'):
            # Questo blocco viene eseguito SOLO se abbiamo una trascrizione valida
            transcript_text, transcript_lang, transcript_type = transcript_result['text'], transcript_result['language'], transcript_result['type']
            current_video_status = 'processing_embedding'
        else:
            # Questo blocco gestisce TUTTI i casi di fallimento (None o dizionario di errore)
            current_video_status = 'failed_transcript'
            transcript_errors += 1
            # Logghiamo il messaggio di errore specifico se presente
            if transcript_result and transcript_result.get('message'):
                logger.warning(f"[CORE YT Process] [{video_id}] Trascrizione fallita. Motivo: {transcript_result['message']}")
    except Exception as e:
        logger.exception(f"Errore imprevisto in /process per video {video_id}: {e}")
        return jsonify({'success': False, 'error_code': 'TRANSCRIPT_UNEXPECTED_ERROR', 'message': f'Unexpected error retrieving transcript: {str(e)}'}), 500

@videos_bp.route('/download_all_transcripts', methods=['GET'])
@login_required
def download_all_transcripts():
    app_mode = current_app.config.get('APP_MODE', 'single')
    db_path = current_app.config.get('DATABASE_FILE')
    logger.info(f"Richiesta download trascrizioni (Modalità: {app_mode})")

    # --- OTTIENI USER ID REALE (se SAAS) ---
    current_user_id = None
    if app_mode == 'saas':
        # Già protetto da @login_required, ma verifichiamo per sicurezza
        if not current_user.is_authenticated: return jsonify({'success': False, 'error_code': 'AUTH_REQUIRED'}), 401
        current_user_id = current_user.id
        logger.info(f"Download trascrizioni per utente '{current_user_id}'")


    if not db_path:
        logger.error("Percorso DATABASE_FILE non configurato per download trascrizioni.")
        return jsonify({'success': False, 'error': 'Server configuration error.'}), 500

    all_transcripts_content = io.StringIO() # Buffer di testo in memoria
    conn = None
    videos_processed_count = 0
    total_chars = 0

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row # Per accedere per nome colonna
        cursor = conn.cursor()

        # --- QUERY CON FILTRO USER_ID SE SAAS ---
        sql_query = "SELECT video_id, title, transcript FROM videos WHERE processing_status = 'completed' AND transcript IS NOT NULL"
        params = []
        if app_mode == 'saas':
           sql_query += " AND user_id = ?" # Aggiungi filtro
           params.append(current_user_id) # Usa ID REALE
        sql_query += " ORDER BY published_at"

        logger.info(f"Esecuzione query per trascrizioni (Filtro Utente: {'Sì' if app_mode=='saas' else 'No'})...")
        cursor.execute(sql_query, tuple(params))

        # Scrivi nel buffer di testo
        for row in cursor.fetchall():
            videos_processed_count += 1
            video_id = row['video_id']
            title = row['title']
            transcript = row['transcript']

            all_transcripts_content.write(f"--- VIDEO START ---\n")
            all_transcripts_content.write(f"ID: {video_id}\n")
            all_transcripts_content.write(f"Titolo: {title}\n")
            all_transcripts_content.write(f"Trascrizione:\n{transcript}\n")
            all_transcripts_content.write(f"--- VIDEO END ---\n\n")
            total_chars += len(transcript) if transcript else 0

        conn.close()
        logger.info(f"Recuperate trascrizioni da {videos_processed_count} video. Totale caratteri: {total_chars}.")

    except sqlite3.Error as e:
        logger.error(f"Errore DB durante recupero trascrizioni: {e}")
        if conn: conn.close()
        return jsonify({'success': False, 'error': 'Database error retrieving transcripts.'}), 500
    except Exception as e:
         logger.error(f"Errore generico durante recupero trascrizioni: {e}", exc_info=True)
         if conn: conn.close()
         return jsonify({'success': False, 'error': 'Unexpected error retrieving transcripts.'}), 500

    # Prepara la risposta come file TXT
    output_filename = f"transcripts_{current_user_id if app_mode=='saas' else 'all'}.txt"
    file_content = all_transcripts_content.getvalue()
    all_transcripts_content.close()

    return Response(
        file_content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment;filename={output_filename}"}
    )

def _reindex_video_from_db(video_id: str, conn: sqlite3.Connection, user_id: Optional[str]) -> str:
    """
    Re-indicizza un singolo video. Se la trascrizione non è nel DB, la scarica da YouTube.
    Questo ora è un wrapper robusto per _reprocess_single_video.
    """
    logger.info(f"[_reindex_video_from_db][{video_id}] Avvio re-indicizzazione/recupero per utente: {user_id}")
    final_status = 'failed_reindex_init'
    cursor = conn.cursor()
    conn.row_factory = sqlite3.Row 
    
    try:
        # Recupera i metadati essenziali dal DB
        sql_get_meta = "SELECT title, channel_id, published_at FROM videos WHERE video_id = ? AND user_id = ?"
        cursor.execute(sql_get_meta, (video_id, user_id))
        video_meta = cursor.fetchone()

        if not video_meta:
            logger.warning(f"[_reindex_video_from_db][{video_id}] Video non trovato nel DB per utente {user_id}.")
            return 'failed_not_found'

        # Recupera la trascrizione da YouTube (logica del pulsante "Riprocessa")
        token_path = current_app.config.get('TOKEN_PATH')
        youtube_client = YouTubeClient(token_file=token_path)
        transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)

        transcript_text, transcript_lang, transcript_type = None, None, None
        if transcript_result and not transcript_result.get('error'):
            transcript_text = transcript_result['text']
            transcript_lang = transcript_result['language']
            transcript_type = transcript_result['type']
        else:
            logger.warning(f"[_reindex_video_from_db][{video_id}] Trascrizione non recuperata da YouTube. Stato: failed_transcript.")
            final_status = 'failed_transcript'
            # Aggiorniamo subito lo stato nel DB e usciamo
            cursor.execute("UPDATE videos SET processing_status = ? WHERE video_id = ?", (final_status, video_id))
            return final_status # Usciamo qui, non c'è altro da fare

        # Se abbiamo la trascrizione, procediamo con l'indicizzazione
        llm_api_key = current_app.config.get('GOOGLE_API_KEY')
        embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
        chunk_size = current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
        chunk_overlap = current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
        base_video_collection_name = current_app.config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
        chroma_client = current_app.config.get('CHROMA_CLIENT')

        user_video_collection_name = f"{base_video_collection_name}_{user_id}"
        video_collection = chroma_client.get_or_create_collection(name=user_video_collection_name)

        # Pulizia vecchi chunk e upsert dei nuovi
        chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        if chunks:
            embeddings = get_gemini_embeddings(chunks, api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_DOCUMENT)
            if embeddings and len(embeddings) == len(chunks):
                video_collection.delete(where={"video_id": video_id})
                
                ids_upsert = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                metadatas_upsert = [{
                    'video_id': video_id, 'video_title': video_meta['title'], 'channel_id': video_meta['channel_id'],
                    'published_at': str(video_meta['published_at']), 'chunk_index': i, 'language': transcript_lang,
                    'caption_type': transcript_type, 'user_id': user_id
                } for i in range(len(chunks))]
                
                video_collection.upsert(ids=ids_upsert, embeddings=embeddings, metadatas=metadatas_upsert, documents=chunks)
                final_status = 'completed'
            else:
                final_status = 'failed_embedding'
        else:
            # Nessun chunk, ma abbiamo la trascrizione. Lo consideriamo completo.
            video_collection.delete(where={"video_id": video_id}) # Puliamo comunque i vecchi chunk
            final_status = 'completed'
        
        # Aggiorna il DB con la trascrizione e lo stato finale
        cursor.execute(
            "UPDATE videos SET transcript = ?, transcript_language = ?, captions_type = ?, processing_status = ? WHERE video_id = ?",
            (transcript_text, transcript_lang, transcript_type, final_status, video_id)
        )
        logger.info(f"[_reindex_video_from_db][{video_id}] DB aggiornato con trascrizione e stato finale '{final_status}'.")

    except Exception as e:
        logger.error(f"[_reindex_video_from_db][{video_id}] Errore critico durante re-indicizzazione: {e}", exc_info=True)
        final_status = 'failed_reindex_critical'
        # Tentiamo di aggiornare lo stato anche in caso di errore critico
        try:
            cursor.execute("UPDATE videos SET processing_status = ? WHERE video_id = ?", (final_status, video_id))
        except sqlite3.Error as db_err:
            logger.error(f"[_reindex_video_from_db][{video_id}] Impossibile aggiornare lo stato DB dopo errore critico: {db_err}")

    return final_status