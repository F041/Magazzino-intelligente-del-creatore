import io
import logging
import sqlite3
import chromadb
from datetime import datetime
from flask import Blueprint, jsonify, request, current_app, Response
from flask_login import login_required, current_user
from typing import Optional
from google.api_core import exceptions as google_exceptions

import threading
import textstat
import copy
from app.services.embedding.embedding_service import generate_embeddings
from app.core.youtube_processor import _background_channel_processing
from app.utils import build_full_config_for_background_process 
from app.services.chunking.agentic_chunker import chunk_text_agentically 
from app.main import load_credentials


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
            args=(app_context, channel_url, current_user_id, copy.deepcopy(initial_status_for_thread), processing_status) # Passa lo stato globale
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
        logger.debug(f"[Reprocess Single] [{video_id}] Costruzione configurazione completa...")
        core_config = build_full_config_for_background_process(current_user_id)
        
        db_path_sqlite = core_config.get('DATABASE_FILE')
        base_video_collection_name = core_config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
        chroma_client = core_config.get('CHROMA_CLIENT')
        
        # Le variabili specifiche per il chunking le leggiamo dopo, quando servono.
        # Qui verifichiamo solo la configurazione di base.
        if not all([db_path_sqlite, base_video_collection_name, chroma_client]):
            raise RuntimeError("Configurazione server incompleta (DB, Collection Name, o Chroma Client).")
        logger.debug(f"[Reprocess Single] [{video_id}] Config OK.")


        # --- 2. Connessione DB e Recupero Metadati FRESCHI da YouTube ---
        logger.debug(f"[Reprocess Single] [{video_id}] Connessione DB: {db_path_sqlite}")
        conn_sqlite = sqlite3.connect(db_path_sqlite)
        cursor_sqlite = conn_sqlite.cursor()

        sql_check_owner = "SELECT video_id FROM videos WHERE video_id = ?"
        params_check_owner = [video_id]
        if app_mode == 'saas':
            sql_check_owner += " AND user_id = ?"
            params_check_owner.append(current_user_id)

        cursor_sqlite.execute(sql_check_owner, tuple(params_check_owner))
        video_exists_for_user = cursor_sqlite.fetchone()

        if not video_exists_for_user:
            message = f"Video ID '{video_id}' non trovato o non appartenente all'utente."
            logger.warning(f"[Reprocess Single] {message}")
            if conn_sqlite: conn_sqlite.close()
            return jsonify({'success': False, 'error_code': 'VIDEO_NOT_FOUND', 'message': message}), 404

        logger.info(f"[{video_id}] Video trovato nel DB. Recupero metadati aggiornati da YouTube...")
        token_path = core_config.get('TOKEN_PATH')
        youtube_client = YouTubeClient(token_file=token_path)
        video_model = youtube_client.get_video_details(video_id)
        video_meta_dict = video_model.model_dump() # Usiamo il modello pydantic per avere i dati puliti
        logger.info(f"[Reprocess Single] [{video_id}] Metadati aggiornati recuperati: {video_meta_dict['title']}")

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
        chunks = []
        if final_status == 'processing_embedding' and transcript_text:
            # --- LOGICA DI CHUNKING CONDIZIONALE ---
            use_agentic_chunking = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'
            chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
            chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)

            if use_agentic_chunking:
                logger.info(f"[Reprocess Single] [{video_id}] Tentativo di CHUNKING INTELLIGENTE (Agentic)...")
                chunks = chunk_text_agentically(transcript_text, llm_provider=core_config.get('llm_provider', 'google'), settings=core_config)
                if not chunks:
                    logger.warning(f"[Reprocess Single] [{video_id}] CHUNKING INTELLIGENTE fallito. Ritorno al metodo classico.")
                    chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            else:
                logger.info(f"[Reprocess Single] [{video_id}] Esecuzione CHUNKING CLASSICO.")
                chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

            if not chunks:
                final_status = 'completed'
                logger.info(f"[Reprocess Single] [{video_id}] Nessun chunk generato dalla trascrizione, marco come completo.")
            else:
                logger.info(f"[Reprocess Single] [{video_id}] Creati {len(chunks)} chunk. Procedo con embedding...")
                try:
                    embeddings = generate_embeddings(chunks, user_settings=core_config, task_type=TASK_TYPE_DOCUMENT)
                    if not embeddings or len(embeddings) != len(chunks):
                        final_status = 'failed_embedding'
                        logger.error(f"[{video_id}] Fallimento generazione/corrispondenza embedding.")
                    else:
                        logger.info(f"[{video_id}] Embedding OK. Preparazione per ChromaDB...")
                        collection_name_for_log = f"{base_video_collection_name}_{current_user_id}" if app_mode == 'saas' else base_video_collection_name
                        chroma_collection_to_use = chroma_client.get_or_create_collection(name=collection_name_for_log)
                        logger.info(f"[{video_id}] Uso collezione Chroma: '{collection_name_for_log}'")
                        
                        chroma_collection_to_use.delete(where={"video_id": video_id})
                        logger.info(f"[{video_id}] Vecchi chunk per il video eliminati da Chroma.")
                        
                        ids_upsert = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                        metadatas_upsert = [{'video_id': video_id, 'channel_id': video_meta_dict['channel_id'], 'video_title': video_meta_dict['title'], 'published_at': str(video_meta_dict['published_at']), 'chunk_index': i, 'language': transcript_lang, 'caption_type': transcript_type, **({"user_id": current_user_id} if app_mode == 'saas' else {})} for i in range(len(chunks))]
                        chroma_collection_to_use.upsert(ids=ids_upsert, embeddings=embeddings, metadatas=metadatas_upsert, documents=chunks)
                        logger.info(f"[{video_id}] Upsert di {len(chunks)} nuovi chunk in Chroma OK.")
                        final_status = 'completed'
                except Exception as e_embed_chroma:
                    logger.exception(f"[{video_id}] Errore durante embedding o operazione ChromaDB.")
                    final_status = 'failed_embedding' # O un codice di errore più specifico
        
        # Pulizia Chroma se trascrizione vuota
        if final_status == 'completed' and not chunks:
            try:
                collection_name_for_log = f"{base_video_collection_name}_{current_user_id}" if app_mode == 'saas' else base_video_collection_name
                chroma_collection_to_use = chroma_client.get_or_create_collection(name=collection_name_for_log)
                chroma_collection_to_use.delete(where={"video_id": video_id})
                logger.info(f"[{video_id}] Pulizia ChromaDB eseguita per video senza nuovi chunk.")
            except Exception as e_chroma_clean:
                logger.error(f"[{video_id}] Errore durante pulizia Chroma per video senza chunk: {e_chroma_clean}")
        # Altri casi (es. trascrizione fallita) mantengono lo stato già impostato

        # --- 6. Aggiornamento Finale SQLite ---
        logger.info(f"[Reprocess Single] [{video_id}] Aggiornamento finale DB. Stato: {final_status}")
        cursor_sqlite.execute( """
            UPDATE videos
            SET title = ?, description = ?, transcript = ?, transcript_language = ?, captions_type = ?, processing_status = ?, added_at = CURRENT_TIMESTAMP
            WHERE video_id = ?
            """,
            (video_meta_dict['title'], video_meta_dict['description'], transcript_text, transcript_lang, transcript_type, final_status, video_id)
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
    if is_overall_success:
        response_data['updated_metadata'] = {
            'title': video_meta_dict['title'],
            'description': video_meta_dict['description'],
            'transcript_preview': (transcript_text[:100] + '...' if transcript_text and len(transcript_text) > 100 else transcript_text) or 'Nessuna',
            'captions_type': transcript_type or 'N/D',
            'transcript_language': transcript_lang or 'N/D'
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

        # Prima cancella i video veri e propri
        cursor_sqlite.execute("DELETE FROM videos WHERE user_id = ?", (current_user_id,))
        rows_affected = cursor_sqlite.rowcount
        
        # ---  ISTRUZIONE DI PULIZIA STATISTICHE ---
        logger.info(f"[{current_user_id}] Eliminazione record corrispondenti da content_stats per 'videos'...")
        cursor_sqlite.execute("DELETE FROM content_stats WHERE user_id = ? AND source_type = 'videos'", (current_user_id,))

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

def _reindex_video_from_db(video_id: str, conn: sqlite3.Connection, user_id: Optional[str], core_config: dict) -> str:
    """
    Re-indicizza un singolo video. Ora gestisce correttamente gli errori di rate limit
    e il context delle variabili locali dopo un'eccezione.
    """
    app_mode = core_config.get('APP_MODE', 'single')
    logger.info(f"[_reindex_video_from_db][{video_id}] Avvio re-indicizzazione per utente: {user_id}")
    
    final_status = 'failed_reindex_init'
    chunking_version_to_set = None
    
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        sql_get_video = "SELECT title, channel_id, published_at, transcript, transcript_language, captions_type FROM videos WHERE video_id = ? AND user_id = ?"
        cursor.execute(sql_get_video, (video_id, user_id))
        video_data = cursor.fetchone()

        if not video_data:
            logger.warning(f"[_reindex_video_from_db][{video_id}] Video non trovato nel DB.")
            return 'failed_not_found'

        video_meta_dict = dict(video_data)
        transcript_text = video_meta_dict.get('transcript')
        
        # --- INIZIO CORREZIONE: Inizializziamo le variabili qui, in modo sicuro ---
        transcript_lang = video_meta_dict.get('transcript_language')
        transcript_type = video_meta_dict.get('captions_type')
        # --- FINE CORREZIONE ---

        if not transcript_text or not transcript_text.strip():
            logger.info(f"[_reindex_video_from_db][{video_id}] Trascrizione non trovata nel DB. Tento il download.")
            try:
                token_path = core_config.get('TOKEN_PATH')
                youtube_client = YouTubeClient(token_file=token_path)
                transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)

                if transcript_result and not transcript_result.get('error'):
                    transcript_text = transcript_result['text']
                    transcript_lang = transcript_result['language'] # Aggiorniamo le variabili
                    transcript_type = transcript_result['type']     # Aggiorniamo le variabili
                    cursor.execute("UPDATE videos SET transcript = ?, transcript_language = ?, captions_type = ? WHERE video_id = ?", (transcript_text, transcript_lang, transcript_type, video_id))
                else:
                    final_status = 'failed_transcript'; cursor.execute("UPDATE videos SET processing_status = ? WHERE video_id = ?", (final_status, video_id)); return final_status
            except Exception as e_yt:
                final_status = 'failed_transcript_api'; cursor.execute("UPDATE videos SET processing_status = ? WHERE video_id = ?", (final_status, video_id)); return final_status
        else:
            logger.info(f"[_reindex_video_from_db][{video_id}] Trascrizione trovata nel DB.")

        use_agentic_chunking = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'
        chunks = []
        if transcript_text and transcript_text.strip():
            if use_agentic_chunking:
                chunks = chunk_text_agentically(transcript_text, llm_provider=core_config.get('llm_provider', 'google'), settings=core_config)
            else:
                chunks = split_text_into_chunks(transcript_text, chunk_size=core_config.get('DEFAULT_CHUNK_SIZE_WORDS', 300), chunk_overlap=core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50))
        
        chroma_client = core_config.get('CHROMA_CLIENT')
        base_video_collection_name = core_config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
        user_video_collection_name = f"{base_video_collection_name}_{user_id}"
        video_collection = chroma_client.get_or_create_collection(name=user_video_collection_name)
        video_collection.delete(where={"video_id": video_id})
        
        if chunks:
            embeddings = generate_embeddings(chunks, user_settings=core_config, task_type=TASK_TYPE_DOCUMENT)
            if embeddings and len(embeddings) == len(chunks):
                ids_upsert = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                metadatas_upsert = [{
                    'video_id': video_id, 'video_title': video_meta_dict['title'], 'channel_id': video_meta_dict['channel_id'],
                    'published_at': str(video_meta_dict['published_at']), 'chunk_index': i, 
                    'language': transcript_lang, # Ora questa variabile esiste sempre
                    'caption_type': transcript_type, # Ora questa variabile esiste sempre
                    'user_id': user_id
                } for i in range(len(chunks))]
                
                video_collection.upsert(ids=ids_upsert, embeddings=embeddings, metadatas=metadatas_upsert, documents=chunks)
                final_status = 'completed'
            else:
                final_status = 'failed_embedding'
        else:
            final_status = 'completed'
        
    except google_exceptions.ResourceExhausted as e:
        logger.warning(f"[_reindex_video_from_db][{video_id}] Rate limit rilevato. Lo segnalo al processo principale.")
        raise e

    except Exception as e:
        logger.error(f"[_reindex_video_from_db][{video_id}] Errore critico durante re-indicizzazione: {e}", exc_info=True)
        final_status = 'failed_reindex_critical'
    
    if final_status == 'completed':
        if use_agentic_chunking:
            rag_models = core_config.get('RAG_MODELS_LIST', [])
            model_name_marker = rag_models[0].strip() if rag_models and rag_models[0].strip() else "unknown_model"
            chunking_version_to_set = f'agentic_v1_{model_name_marker}'
        else:
            chunking_version_to_set = 'classic_v1'
    
    cursor.execute("UPDATE videos SET processing_status = ?, chunking_version = ? WHERE video_id = ?", (final_status, chunking_version_to_set, video_id))
    
    logger.info(f"[_reindex_video_from_db][{video_id}] Re-indicizzazione terminata con stato: {final_status}")
    return final_status