# FILE: app/api/routes/documents.py (Sostituisci l'intera funzione upload_documents)

import logging
from flask import Blueprint, request, jsonify, current_app
from werkzeug.utils import secure_filename
from flask_login import login_required, current_user
import os
import uuid
import threading
import sqlite3
import docx
from typing import Optional
from pypdf import PdfReader
# from markdownify import markdownify as md
# Opzionale, se estraiamo HTML e vogliamo MD
from google.api_core import exceptions as google_exceptions
from app.services.embedding.embedding_service import generate_embeddings
from app.services.chunking.agentic_chunker import chunk_text_agentically
from app.utils import build_full_config_for_background_process

logger = logging.getLogger(__name__)  

try:
    from app.services.embedding.gemini_embedding import split_text_into_chunks, get_gemini_embeddings, TASK_TYPE_DOCUMENT
except ImportError:
    # Fallback se la struttura è leggermente diversa, aggiusta se necessario
    logger.error("!!! Impossibile importare funzioni di embedding/chunking !!!")
    split_text_into_chunks = None
    get_gemini_embeddings = None
    TASK_TYPE_DOCUMENT = "retrieval_document" # Definisci comunque la costante


documents_bp = Blueprint('documents', __name__)

def allowed_file(filename):
    """Controlla se l'estensione del file è permessa leggendo dalla config."""
    allowed_extensions = current_app.config.get('ALLOWED_EXTENSIONS', {'txt', 'pdf', 'docx'}) # Aggiunto docx
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed_extensions

# --- Funzione Helper per Estrarre Testo ---
def extract_text_from_file(file_storage, original_filename):
    """
    Estrae il testo da un oggetto FileStorage in base all'estensione.
    Restituisce il testo estratto o None se fallisce o tipo non supportato.
    """
    filename = secure_filename(original_filename) # Usa il nome sicuro per il log
    text_content = None
    file_ext = filename.rsplit('.', 1)[1].lower()

    try:
        if file_ext == 'txt':
            # Legge il file TXT, decodificando in UTF-8 (gestisce errori)
            text_content = file_storage.read().decode('utf-8', errors='replace')
            logger.info(f"Estratto testo da TXT: {filename}")
        elif file_ext == 'docx':
            # Usa python-docx per leggere il contenuto
            # file_storage.stream è necessario perché python-docx lavora con stream/path
            doc = docx.Document(file_storage.stream)
            full_text = [para.text for para in doc.paragraphs]
            text_content = '\n'.join(full_text)
            logger.info(f"Estratto testo da DOCX: {filename}")
        elif file_ext == 'pdf':
            # Usa pypdf per leggere il testo pagina per pagina
            reader = PdfReader(file_storage.stream)
            full_text = []
            for i, page in enumerate(reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                         full_text.append(page_text)
                except Exception as page_err:
                     logger.warning(f"Errore estrazione testo da pagina {i+1} di {filename}: {page_err}")
            text_content = '\n\n'.join(full_text) # Separa pagine con doppia newline
            logger.info(f"Estratto testo da PDF: {filename} ({len(reader.pages)} pagine)")
        else:
            logger.warning(f"Tipo file non supportato per estrazione testo: {filename}")
            return None # Tipo non gestito

        # Semplice normalizzazione (puoi espanderla)
        if text_content:
             text_content = '\n'.join(line.strip() for line in text_content.splitlines() if line.strip()) # Rimuove righe vuote e spazi iniziali/finali

        return text_content

    except Exception as e:
        logger.error(f"Errore durante estrazione testo da {filename}: {e}", exc_info=True)
        return None # Fallimento estrazione

def _index_document(doc_id: str, conn: sqlite3.Connection, user_id: Optional[str], core_config: dict) -> str:
    """
    Esegue l'indicizzazione di un documento (lettura MD, chunk, embed, Chroma).
    Gestisce modalità 'single' e 'saas'.
    NON fa commit; si aspetta che il chiamante gestisca la transazione.
    Restituisce lo stato finale ('completed' o 'failed_...').
    """
    app_mode = core_config.get('APP_MODE', 'single')
    logger.info(f"[_index_document][{doc_id}] Avvio indicizzazione (Modalità: {app_mode}, UserID: {user_id if user_id else 'N/A'})")

    final_status = 'failed_indexing_init'
    cursor = conn.cursor()

    # Recupera Configurazione Essenziale
    embedding_model = core_config.get('GEMINI_EMBEDDING_MODEL')
    chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
    chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
    base_doc_collection_name = core_config.get('DOCUMENT_COLLECTION_NAME', 'document_content')
    use_agentic_chunking = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'


    # Ottieni Collezione ChromaDB
    doc_collection = None
    collection_name_for_log = "N/A"
    
    chroma_client = core_config.get('CHROMA_CLIENT')
    if not chroma_client: 
        logger.error(f"[_index_document][{doc_id}] Chroma Client non trovato in core_config!")
        return 'failed_config_client_missing'

    if app_mode == 'single':
        try:
            doc_collection = chroma_client.get_or_create_collection(name=base_doc_collection_name)
            if doc_collection: collection_name_for_log = doc_collection.name
        except Exception as e:
             logger.error(f"[_index_document][{doc_id}] Modalità SINGLE: Errore get/create collezione: {e}")
             return 'failed_config_collection_missing'
    elif app_mode == 'saas':
        if not user_id: 
            logger.error(f"[_index_document][{doc_id}] Modalità SAAS: User ID mancante!")
            return 'failed_user_id_missing'
        
        user_doc_collection_name = f"{base_doc_collection_name}_{user_id}"
        collection_name_for_log = user_doc_collection_name
        try:
            doc_collection = chroma_client.get_or_create_collection(name=user_doc_collection_name)
        except Exception as e_saas_coll: 
            logger.error(f"[_index_document][{doc_id}] Modalità SAAS: Errore get/create collezione '{user_doc_collection_name}': {e_saas_coll}")
            return 'failed_chroma_collection_saas'
    else: 
        logger.error(f"[_index_document][{doc_id}] Modalità APP non valida: {app_mode}")
        return 'failed_invalid_mode'

    if not doc_collection: 
        logger.error(f"[_index_document][{doc_id}] Fallimento ottenimento collezione ChromaDB (Nome tentato: {collection_name_for_log}).")
        return 'failed_chroma_collection_generic'
    
    logger.info(f"[_index_document][{doc_id}] Collezione Chroma '{collection_name_for_log}' pronta.")

    if not embedding_model: logger.error(f"[_index_document][{doc_id}] Configurazione Embedding mancante."); return 'failed_config_embedding'
    if not split_text_into_chunks: logger.error(f"[_index_document][{doc_id}] Funzione chunking base non disponibile."); return 'failed_server_setup'


    # Logica Indicizzazione
    try:
        cursor.execute("SELECT content, original_filename FROM documents WHERE doc_id = ?", (doc_id,))
        doc_data = cursor.fetchone()
        if not doc_data: 
            logger.error(f"[_index_document][{doc_id}] Record non trovato nel DB.")
            return 'failed_doc_not_found'
        
        markdown_content, original_filename = doc_data[0], doc_data[1]
        logger.info(f"[_index_document][{doc_id}] Trovato documento '{original_filename}' nel DB.")

        if not markdown_content or not markdown_content.strip():
             logger.warning(f"[_index_document][{doc_id}] File Markdown vuoto. Marco come completato.")
             final_status = 'completed'
        else:
             logger.info(f"[_index_document][{doc_id}] Contenuto letto ({len(markdown_content)} chars).")
             
             chunks = []
             if use_agentic_chunking:
                logger.info(f"[_index_document][{doc_id}] Tentativo di CHUNKING INTELLIGENTE (Agentic)...")
                # Passiamo l'INTERO dizionario core_config, che contiene TUTTO.
                chunks = chunk_text_agentically(
                    markdown_content, 
                    llm_provider=core_config.get('llm_provider', 'google'), 
                    settings=core_config
                )
                
                if not chunks:
                    logger.warning(f"[_index_document][{doc_id}] CHUNKING INTELLIGENTE fallito o ha restituito 0 chunk. Ritorno al metodo classico.")
                    chunks = split_text_into_chunks(markdown_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
             else:
                logger.info(f"[_index_document][{doc_id}] Esecuzione CHUNKING CLASSICO (basato su dimensione).")
                chunks = split_text_into_chunks(markdown_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

             if not chunks:
                 logger.info(f"[_index_document][{doc_id}] Nessun chunk generato. Marco come completato.")
                 final_status = 'completed'
             else:
                 logger.info(f"[_index_document][{doc_id}] Creati {len(chunks)} chunk.")
                 embeddings = generate_embeddings(chunks, user_settings=core_config, task_type=TASK_TYPE_DOCUMENT)
                 if not embeddings or len(embeddings) != len(chunks):
                     logger.error(f"[_index_document][{doc_id}] Fallimento generazione embedding.")
                     final_status = 'failed_embedding'
                 else:
                     logger.info(f"[_index_document][{doc_id}] Embedding generati.")
                     logger.info(f"[_index_document][{doc_id}] Salvataggio in ChromaDB ({doc_collection.name})...")
                     ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
                     metadatas_chroma = [{
                         "doc_id": doc_id, "original_filename": original_filename,
                         "chunk_index": i, "source_type": "document",
                         **({"user_id": user_id} if app_mode == 'saas' and user_id else {})
                     } for i in range(len(chunks))]
                     doc_collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas_chroma, documents=chunks)
                     logger.info(f"[_index_document][{doc_id}] Salvataggio ChromaDB completato.")
                     final_status = 'completed' # Successo!

    except Exception as e:
        logger.error(f"[_index_document][{doc_id}] Errore imprevisto durante indicizzazione: {e}", exc_info=True)
        if final_status not in ['completed', 'failed_embedding']:
             if isinstance(e, FileNotFoundError): final_status = 'failed_file_not_found'
             elif isinstance(e, IOError): final_status = 'failed_reading_file'
             elif 'split_text_into_chunks' in str(e): final_status = 'failed_chunking'
             elif 'upsert' in str(e): final_status = 'failed_chroma_write'
             else: final_status = 'failed_processing_generic'

    if final_status == 'completed':
        try:
            import textstat
            stats = { 'word_count': 0, 'gunning_fog': 0 }
            if markdown_content and markdown_content.strip():
                stats['word_count'] = len(markdown_content.split())
                stats['gunning_fog'] = textstat.gunning_fog(markdown_content)

            # Inserisce o aggiorna le statistiche nella tabella cache
            cursor.execute("""
                INSERT INTO content_stats (content_id, user_id, source_type, word_count, gunning_fog)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET
                    word_count = excluded.word_count,
                    gunning_fog = excluded.gunning_fog,
                    last_calculated = CURRENT_TIMESTAMP
            """, (doc_id, user_id, 'document', stats['word_count'], stats['gunning_fog']))
            logger.info(f"[_index_document][{doc_id}] Statistiche salvate/aggiornate nella cache.")
        except Exception as e_stats:
            logger.error(f"[_index_document][{doc_id}] Errore durante il calcolo/salvataggio delle statistiche: {e_stats}")
            # Non blocchiamo il processo per questo, ma lo registriamo

    # Aggiorna stato DB
    try:
        logger.info(f"[_index_document][{doc_id}] Aggiornamento stato DB a '{final_status}'...")
        cursor.execute("UPDATE documents SET processing_status = ? WHERE doc_id = ?", (final_status, doc_id))
    except sqlite3.Error as db_update_err:
         logger.error(f"[_index_document][{doc_id}] ERRORE CRITICO aggiornamento stato finale DB: {db_update_err}")
         final_status = 'failed_db_status_update'

    logger.info(f"[_index_document][{doc_id}] Indicizzazione terminata con stato restituito: {final_status}")
    return final_status


# --- Endpoint Upload
@documents_bp.route('/upload', methods=['POST'])
@login_required
def upload_documents():
    """
    Riceve file, estrae testo, salva come .md, REGISTRA nel DB (con user_id se saas)
    e AVVIA L'INDICIZZAZIONE (_index_document) automaticamente.
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"Richiesta upload documenti (Modalità: {app_mode})")

    current_user_id = current_user.id if app_mode == 'saas' and current_user.is_authenticated else None
    if app_mode == 'saas':
        logger.info(f"Upload per utente '{current_user_id}'")

    upload_folder = current_app.config.get('UPLOAD_FOLDER_PATH')
    db_path = current_app.config.get('DATABASE_FILE')
    if not upload_folder or not db_path:
        logger.error("Configurazione UPLOAD_FOLDER_PATH o DATABASE_FILE mancante!")
        return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Errore configurazione server (upload path o db path).'}), 500
    try:
        os.makedirs(upload_folder, exist_ok=True)
    except OSError as e:
        logger.error(f"Impossibile creare la directory di upload {upload_folder}: {e}")
        return jsonify({'success': False, 'error_code': 'UPLOAD_DIR_ERROR', 'message': 'Errore creazione directory di upload sul server.'}), 500

    if 'documents' not in request.files:
        return jsonify({'success': False, 'error_code': 'NO_FILE_PART', 'message': "Nessuna parte 'documents' nella richiesta."}), 400

    uploaded_files = request.files.getlist('documents')
    if not uploaded_files or all(f.filename == '' for f in uploaded_files):
         return jsonify({'success': False, 'error_code': 'NO_FILE_SELECTED', 'message': 'Nessun file selezionato per il caricamento.'}), 400

    processed_ok_info, processed_fail_info, skipped_files_info = [], [], []
    conn = None

    try:
        logger.debug(f"Connessione a DB: {db_path}")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # COSTRUISCI LA CONFIGURAZIONE UNA SOLA VOLTA
        core_config_dict = build_full_config_for_background_process(current_user_id)

        for file in uploaded_files:
            doc_id = None
            original_filename = secure_filename(file.filename) if file and file.filename else 'N/A'

            if file and file.filename and allowed_file(original_filename):
                file.seek(0, os.SEEK_END) # Vai alla fine del file
                file_size = file.tell()   # Leggi la posizione (che è la dimensione in byte)
                file.seek(0)              # Torna all'inizio per la lettura successiva
                extracted_text = extract_text_from_file(file, original_filename)

                if extracted_text is not None:
                    doc_id = str(uuid.uuid4())
                    original_mimetype = file.mimetype

                    try:
                        sql_insert_doc = """
                            INSERT INTO documents (doc_id, original_filename, content, mimetype, user_id, processing_status, filesize) 
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """
                        values_to_insert = (doc_id, original_filename, extracted_text, original_mimetype, current_user_id, 'pending', file_size)
                        cursor.execute(sql_insert_doc, values_to_insert)


                        indexing_status = _index_document(doc_id, conn, current_user_id, core_config_dict)

                        if indexing_status == 'completed':
                            processed_ok_info.append({ 'doc_id': doc_id, 'original_filename': original_filename, 'final_status': indexing_status })
                        else:
                            processed_fail_info.append({ 'doc_id': doc_id, 'original_filename': original_filename, 'final_status': indexing_status })

                    except sqlite3.Error as db_err:
                        logger.error(f"Errore DB inserendo record per {original_filename}: {db_err}")
                        skipped_files_info.append({'filename': original_filename, 'error': 'Database error.'})
                    except Exception as process_err:
                         logger.error(f"Errore imprevisto durante processamento {original_filename}: {process_err}", exc_info=True)
                         skipped_files_info.append({'filename': original_filename, 'error': f'Unexpected error: {process_err}'})
                else:
                     logger.warning(f"Estrazione testo fallita per '{original_filename}'. File scartato.")
                     skipped_files_info.append({'filename': original_filename, 'error': 'Text extraction failed.'})
            elif file and file.filename:
                logger.warning(f"File '{original_filename}' scartato: estensione non permessa.")
                skipped_files_info.append({'filename': original_filename, 'error': 'File type not allowed'})
        
        conn.commit()

    except sqlite3.Error as e:
        logger.error(f"Errore SQLite esterno al ciclo file: {e}")
        if conn: conn.rollback()
        return jsonify({'success': False, 'error_code': 'DB_OPERATION_ERROR', 'message': f'Errore database: {e}'}), 500
    except Exception as e_outer:
         logger.error(f"Errore generico imprevisto durante upload: {e_outer}", exc_info=True)
         if conn: conn.rollback()
         return jsonify({'success': False, 'error_code': 'UNEXPECTED_UPLOAD_ERROR', 'message': f'Errore server imprevisto: {e_outer}'}), 500
    finally:
        if conn: conn.close()

    overall_success = len(processed_ok_info) > 0 or (len(processed_fail_info) == 0 and len(skipped_files_info) == 0)
    final_message = f"Operazione completata. {len(processed_ok_info)} indicizzati, {len(processed_fail_info)} con errori, {len(skipped_files_info)} scartati."
    return jsonify({
        'success': overall_success, 'message': final_message, 'files_indexed_ok': processed_ok_info,
        'files_indexing_failed': processed_fail_info, 'files_skipped': skipped_files_info
    }), 200 if overall_success else 400



@documents_bp.route('/<string:doc_id>', methods=['DELETE'])
@login_required
def delete_document(doc_id):
    """
    Elimina un documento specifico (record DB, chunks Chroma E STATISTICHE).
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"Richiesta DELETE ricevuta per doc_id: {doc_id} (Modalità: {app_mode})")

    current_user_id = current_user.id
    logger.info(f"[{doc_id}] Tentativo eliminazione per utente '{current_user_id}'")

    db_path = current_app.config.get('DATABASE_FILE')
    base_doc_collection_name = current_app.config.get('DOCUMENT_COLLECTION_NAME', 'document_content')
    if not db_path or not base_doc_collection_name:
        logger.error("Configurazione DATABASE_FILE o DOCUMENT_COLLECTION_NAME mancante per delete_document.")
        return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Errore configurazione server.'}), 500

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        sql_select = "SELECT doc_id FROM documents WHERE doc_id = ? AND user_id = ?"
        params_select = (doc_id, current_user_id)
        cursor.execute(sql_select, params_select)
        result = cursor.fetchone()

        if result is None:
            conn.close()
            message = f"Documento {doc_id} non trovato per l'utente {current_user_id}."
            logger.warning(message)
            return jsonify({'success': False, 'error_code': 'DOCUMENT_NOT_FOUND', 'message': message}), 404

        logger.info(f"[{doc_id}] Trovato documento. Procedo con l'eliminazione.")

        chroma_delete_success = False
        try:
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            if chroma_client:
                user_doc_collection_name = f"{base_doc_collection_name}_{current_user_id}"
                try:
                   doc_collection = chroma_client.get_collection(name=user_doc_collection_name)
                   chunks_to_delete = doc_collection.get(where={"doc_id": doc_id}, include=[])
                   chunk_ids_to_delete = chunks_to_delete.get('ids', [])
                   if chunk_ids_to_delete:
                       doc_collection.delete(ids=chunk_ids_to_delete)
                   chroma_delete_success = True
                except Exception:
                   chroma_delete_success = True
        except Exception as e_chroma:
            logger.error(f"[{doc_id}] ERRORE durante l'eliminazione da ChromaDB: {e_chroma}", exc_info=True)

        # Elimina da SQLite
        sql_delete_doc = "DELETE FROM documents WHERE doc_id = ? AND user_id = ?"
        cursor.execute(sql_delete_doc, (doc_id, current_user_id))
        
        # ---  ISTRUZIONE DI PULIZIA STATISTICHE ---
        logger.info(f"[{doc_id}] Eliminazione record corrispondente da content_stats...")
        sql_delete_stats = "DELETE FROM content_stats WHERE content_id = ? AND user_id = ?"
        cursor.execute(sql_delete_stats, (doc_id, current_user_id))
        
        if cursor.rowcount == 0:
             logger.warning(f"[{doc_id}] Il DELETE da documents non ha modificato righe (potrebbe essere già stato cancellato).")

        conn.commit()
        conn.close()

        final_message = f"Documento {doc_id} eliminato con successo dal database e dalle statistiche."
        if not chroma_delete_success:
            final_message += " ATTENZIONE: Errore durante pulizia ChromaDB."
            
        return jsonify({
            'success': True,
            'message': final_message,
            'doc_id': doc_id,
            'chroma_delete_success': chroma_delete_success
            }), 200

    except Exception as e_outer:
         logger.error(f"Errore generico imprevisto durante eliminazione doc {doc_id}: {e_outer}", exc_info=True)
         if conn: conn.rollback(); conn.close()
         return jsonify({'success': False, 'error_code': 'UNEXPECTED_DELETE_ERROR', 'message': f'Errore server imprevisto.'}), 500