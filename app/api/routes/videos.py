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
                    user_settings_for_embedding = {'llm_provider': 'google', 'llm_api_key': llm_api_key, 'llm_embedding_model': embedding_model}
                    embeddings = generate_embeddings(chunks, user_settings=user_settings_for_embedding, task_type=TASK_TYPE_DOCUMENT)

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
    Re-indicizza un singolo video. USA LA TRASCRIZIONE DAL DB SE ESISTE.
    Se non esiste, la scarica da YouTube.
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_reindex_video_from_db][{video_id}] Avvio re-indicizzazione intelligente per utente: {user_id}")
    
    final_status = 'failed_reindex_init'
    cursor = conn.cursor()
    # Aggiungiamo il row_factory qui per assicurarci di poter accedere ai dati per nome
    conn.row_factory = sqlite3.Row

    try:
        # 1. Recupera TUTTI i dati del video dal DB, inclusa la trascrizione (se c'è)
        sql_get_video = "SELECT title, channel_id, published_at, transcript, transcript_language, captions_type FROM videos WHERE video_id = ? AND user_id = ?"
        cursor.execute(sql_get_video, (video_id, user_id))
        video_data = cursor.fetchone()

        if not video_data:
            logger.warning(f"[_reindex_video_from_db][{video_id}] Video non trovato nel DB per utente {user_id}.")
            return 'failed_not_found'

        video_meta_dict = dict(video_data)
        transcript_text = video_meta_dict.get('transcript')
        transcript_lang = video_meta_dict.get('transcript_language')
        transcript_type = video_meta_dict.get('captions_type')

        # 2. Logica "intelligente": scarica la trascrizione SOLO se non è presente nel DB
        if not transcript_text or not transcript_text.strip():
            logger.info(f"[_reindex_video_from_db][{video_id}] Trascrizione non trovata nel DB. Tento il download da YouTube (costo: 200 punti).")
            try:
                token_path = current_app.config.get('TOKEN_PATH')
                youtube_client = YouTubeClient(token_file=token_path)
                transcript_result = TranscriptService.get_transcript(video_id, youtube_client=youtube_client)

                if transcript_result and not transcript_result.get('error'):
                    transcript_text = transcript_result['text']
                    transcript_lang = transcript_result['language']
                    transcript_type = transcript_result['type']
                    # Aggiorniamo subito il DB con la trascrizione appena scaricata
                    cursor.execute(
                        "UPDATE videos SET transcript = ?, transcript_language = ?, captions_type = ? WHERE video_id = ?",
                        (transcript_text, transcript_lang, transcript_type, video_id)
                    )
                    logger.info(f"[_reindex_video_from_db][{video_id}] Trascrizione scaricata e salvata nel DB.")
                else:
                    logger.warning(f"[_reindex_video_from_db][{video_id}] Download da YouTube fallito. Stato: failed_transcript.")
                    final_status = 'failed_transcript'
                    cursor.execute("UPDATE videos SET processing_status = ? WHERE video_id = ?", (final_status, video_id))
                    return final_status # Usciamo, non possiamo continuare
            except Exception as e_yt:
                logger.error(f"[_reindex_video_from_db][{video_id}] Errore critico durante il download da YouTube: {e_yt}", exc_info=True)
                final_status = 'failed_transcript_api'
                cursor.execute("UPDATE videos SET processing_status = ? WHERE video_id = ?", (final_status, video_id))
                return final_status
        else:
            logger.info(f"[_reindex_video_from_db][{video_id}] Trascrizione trovata nel DB. Procedo a costo zero.")

        # 3. Se arriviamo qui, abbiamo una trascrizione (dal DB o da YouTube) e possiamo procedere
        llm_api_key = current_app.config.get('GOOGLE_API_KEY')
        embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
        chunk_size = current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
        chunk_overlap = current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
        base_video_collection_name = current_app.config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
        chroma_client = current_app.config.get('CHROMA_CLIENT')

        user_video_collection_name = f"{base_video_collection_name}_{user_id}"
        video_collection = chroma_client.get_or_create_collection(name=user_video_collection_name)
        
        # Puliamo i vecchi chunk prima di inserire i nuovi
        video_collection.delete(where={"video_id": video_id})

        chunks = split_text_into_chunks(transcript_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        if chunks:
                user_settings_for_embedding = {'llm_provider': 'google', 'llm_api_key': llm_api_key, 'llm_embedding_model': embedding_model}
                embeddings = generate_embeddings(chunks, user_settings=user_settings_for_embedding, task_type=TASK_TYPE_DOCUMENT)
                if embeddings and len(embeddings) == len(chunks):
                    ids_upsert = [f"{video_id}_chunk_{i}" for i in range(len(chunks))]
                    metadatas_upsert = [{
                        'video_id': video_id, 'video_title': video_meta_dict['title'], 'channel_id': video_meta_dict['channel_id'],
                        'published_at': str(video_meta_dict['published_at']), 'chunk_index': i, 'language': transcript_lang,
                        'caption_type': transcript_type, 'user_id': user_id
                    } for i in range(len(chunks))]
                    
                    video_collection.upsert(ids=ids_upsert, embeddings=embeddings, metadatas=metadatas_upsert, documents=chunks)
                    final_status = 'completed'
                else:
                    final_status = 'failed_embedding'
        else:
            final_status = 'completed' # Nessun chunk da indicizzare, ma l'operazione è OK

    except Exception as e:
        logger.error(f"[_reindex_video_from_db][{video_id}] Errore critico durante re-indicizzazione: {e}", exc_info=True)
        final_status = 'failed_reindex_critical'
    
    # Aggiorna lo stato finale nel DB (NON fa commit, se lo aspetta dal chiamante)
    cursor.execute("UPDATE videos SET processing_status = ? WHERE video_id = ?", (final_status, video_id))
    logger.info(f"[_reindex_video_from_db][{video_id}] Re-indicizzazione terminata con stato: {final_status}")
    return final_status