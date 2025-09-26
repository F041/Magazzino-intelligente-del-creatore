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

  

try:
    from app.services.embedding.gemini_embedding import split_text_into_chunks, get_gemini_embeddings, TASK_TYPE_DOCUMENT
except ImportError:
    # Fallback se la struttura è leggermente diversa, aggiusta se necessario
    logger.error("!!! Impossibile importare funzioni di embedding/chunking !!!")
    split_text_into_chunks = None
    get_gemini_embeddings = None
    TASK_TYPE_DOCUMENT = "retrieval_document" # Definisci comunque la costante

logger = logging.getLogger(__name__)
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

def _index_document(doc_id: str, conn: sqlite3.Connection, user_id: Optional[str] = None) -> str:
    """
    Esegue l'indicizzazione di un documento (lettura MD, chunk, embed, Chroma).
    Gestisce modalità 'single' e 'saas'.
    NON fa commit; si aspetta che il chiamante gestisca la transazione.
    Restituisce lo stato finale ('completed' o 'failed_...').
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_index_document][{doc_id}] Avvio indicizzazione (Modalità: {app_mode}, UserID: {user_id if user_id else 'N/A'})")

    final_status = 'failed_indexing_init'
    cursor = conn.cursor()

    # Recupera Configurazione Essenziale
    llm_api_key = current_app.config.get('GOOGLE_API_KEY')
    embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
    chunk_size = current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
    chunk_overlap = current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
    base_doc_collection_name = current_app.config.get('DOCUMENT_COLLECTION_NAME', 'document_content')

    # Ottieni Collezione ChromaDB
    doc_collection = None
    collection_name_for_log = "N/A"

    if app_mode == 'single':
        doc_collection = current_app.config.get('CHROMA_DOC_COLLECTION')
        if doc_collection: collection_name_for_log = doc_collection.name
        else: logger.error(f"[_index_document][{doc_id}] Modalità SINGLE: Collezione documenti non trovata!"); return 'failed_config_collection_missing'
    elif app_mode == 'saas':
        if not user_id: logger.error(f"[_index_document][{doc_id}] Modalità SAAS: User ID mancante!"); return 'failed_user_id_missing'
        chroma_client = current_app.config.get('CHROMA_CLIENT')
        if not chroma_client: logger.error(f"[_index_document][{doc_id}] Modalità SAAS: Chroma Client non trovato!"); return 'failed_config_client_missing'
        user_doc_collection_name = f"{base_doc_collection_name}_{user_id}"
        collection_name_for_log = user_doc_collection_name
        try:
            logger.info(f"[_index_document][{doc_id}] Modalità SAAS: Ottenimento/Creazione collezione '{user_doc_collection_name}'...")
            doc_collection = chroma_client.get_or_create_collection(name=user_doc_collection_name)
        except Exception as e_saas_coll: logger.error(f"[_index_document][{doc_id}] Modalità SAAS: Errore get/create collezione '{user_doc_collection_name}': {e_saas_coll}"); return 'failed_chroma_collection_saas'
    else: logger.error(f"[_index_document][{doc_id}] Modalità APP non valida: {app_mode}"); return 'failed_invalid_mode'

    if not doc_collection: logger.error(f"[_index_document][{doc_id}] Fallimento ottenimento collezione ChromaDB (Nome tentato: {collection_name_for_log})."); return 'failed_chroma_collection_generic'
    logger.info(f"[_index_document][{doc_id}] Collezione Chroma '{collection_name_for_log}' pronta.")

    if not llm_api_key or not embedding_model: logger.error(f"[_index_document][{doc_id}] Configurazione Embedding mancante."); return 'failed_config_embedding'
    if not split_text_into_chunks or not get_gemini_embeddings: logger.error(f"[_index_document][{doc_id}] Funzioni chunk/embed non disponibili."); return 'failed_server_setup'

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
             chunks = split_text_into_chunks(markdown_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
             if not chunks:
                 logger.info(f"[_index_document][{doc_id}] Nessun chunk generato. Marco come completato.")
                 final_status = 'completed'
             else:
                 logger.info(f"[_index_document][{doc_id}] Creati {len(chunks)} chunk.")
                 user_settings_for_embedding = {'llm_provider': 'google', 'llm_api_key': llm_api_key, 'llm_embedding_model': embedding_model}
                 embeddings = generate_embeddings(chunks, user_settings=user_settings_for_embedding, task_type=TASK_TYPE_DOCUMENT)
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

    # Gestione User ID (Temporaneo per SAAS)
    current_user_id = None
    if app_mode == 'saas':
        if not current_user.is_authenticated: return jsonify(...), 401 # Già gestito da @login_required ma doppia sicurezza
        current_user_id = current_user.id # <<< USA ID REALE
        logger.info(f"Upload per utente '{current_user_id}'")

    # Setup e Validazioni Iniziali
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

    # Processamento File
    processed_ok_info, processed_fail_info, skipped_files_info = [], [], []
    conn = None

    try:
        logger.debug(f"Connessione a DB: {db_path}")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        for file in uploaded_files:
            doc_id = None; md_filepath = None
            original_filename = file.filename if file else 'N/A'

            if file and file.filename and allowed_file(original_filename):
                extracted_text = extract_text_from_file(file, original_filename)

                if extracted_text is not None:
                    doc_id = str(uuid.uuid4())
                    stored_md_filename = f"{doc_id}.md"
                    
                    # Percorso completo per salvare il file
                    full_md_filepath = os.path.join(upload_folder, stored_md_filename)
                    # PERCORSO RELATIVO da salvare nel DB
                    relative_md_filepath = os.path.join(os.path.basename(os.path.dirname(upload_folder)), stored_md_filename)

                    original_mimetype = file.mimetype
                    md_filesize = 0

                    # ---- BLOCCO TRY/EXCEPT PRINCIPALE PER IL SINGOLO FILE ----
                    try:
                        # Salva MD usando il percorso completo
                        with open(full_md_filepath, 'w', encoding='utf-8') as f_md:
                            f_md.write(extracted_text)
                        md_filesize = os.path.getsize(full_md_filepath)
                        logger.info(f"File '{original_filename}' salvato come MD: {stored_md_filename}")

                        # INSERT SQLite (con user_id e contenuto)
                        sql_insert_doc = """
                            INSERT INTO documents (
                                doc_id, original_filename, content,
                                filesize, mimetype, user_id, processing_status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """
                        values_to_insert = (
                            doc_id,
                            original_filename,
                            extracted_text, # Inseriamo il testo estratto
                            md_filesize,
                            original_mimetype,
                            current_user_id,
                            'pending'
                        )

                        cursor.execute(sql_insert_doc, values_to_insert)
                        logger.info(f"Record DB inserito per doc_id: {doc_id} (UserID: {current_user_id if current_user_id else 'None'}, stato pending)")

                        # AVVIA INDICIZZAZIONE (con user_id)
                        indexing_status = _index_document(doc_id, conn, current_user_id)

                        # Categorizza risultato
                        if indexing_status == 'completed':
                            processed_ok_info.append({ 'doc_id': doc_id, 'original_filename': original_filename, 'final_status': indexing_status })
                        else:
                            processed_fail_info.append({ 'doc_id': doc_id, 'original_filename': original_filename, 'final_status': indexing_status })

                    # ---- GESTORI ECCEZIONI per il try sopra ----
                    except sqlite3.Error as db_err: # Errore specifico DB
                        logger.error(f"Errore DB inserendo record iniziale per {original_filename}: {db_err}")
                        skipped_files_info.append({'filename': original_filename, 'error': 'Database error during initial registration.'})
                        # Tentativo pulizia file MD
                        if 'md_filepath' in locals() and md_filepath and os.path.exists(md_filepath):
                            try:
                                os.remove(md_filepath)
                                logger.info(f"File MD rimosso ({md_filepath}) dopo fallimento INSERT DB.")
                            except OSError as remove_err:
                                logger.warning(f"Fallito tentativo rimozione file MD {md_filepath} dopo fallimento INSERT DB: {remove_err}")

                    except Exception as save_err: # Altri errori (I/O, _index_document, etc.)
                         logger.error(f"Errore imprevisto salvataggio/registrazione/indicizzazione {original_filename}: {save_err}", exc_info=True)
                         skipped_files_info.append({'filename': original_filename, 'error': f'Unexpected error during file processing: {save_err}'})
                         # Tentativo pulizia file MD
                         if 'md_filepath' in locals() and md_filepath and os.path.exists(md_filepath):
                             try:
                                 os.remove(md_filepath)
                                 logger.info(f"File MD parziale rimosso ({md_filepath}) dopo errore.")
                             except OSError as remove_err:
                                 logger.warning(f"Fallito tentativo rimozione file MD {md_filepath} dopo errore: {remove_err}")
                    # ---- FINE GESTORI ECCEZIONI ----

                else: # Questo else corrisponde a: if extracted_text is not None:
                     logger.warning(f"Estrazione testo fallita per '{original_filename}'. File scartato.")
                     skipped_files_info.append({'filename': original_filename, 'error': 'Text extraction failed.'})

            elif file and file.filename: # Questo elif corrisponde a: if file and file.filename and allowed_file(original_filename):
                logger.warning(f"File '{original_filename}' scartato: estensione non permessa.")
                skipped_files_info.append({'filename': original_filename, 'error': 'File type not allowed'})
            else:
                 logger.warning("Trovato un file invalido o senza nome nella lista.")
        # ---- FINE CICLO FOR file in uploaded_files ----

        # Commit DB alla FINE
        conn.commit()
        total_processed = len(processed_ok_info) + len(processed_fail_info)
        logger.info(f"Commit DB eseguito. Totale file processati (OK+Fail): {total_processed}, Scartati: {len(skipped_files_info)}")

    except sqlite3.Error as e:
        logger.error(f"Errore SQLite esterno al ciclo file: {e}")
        if conn: conn.rollback()
        return jsonify({'success': False, 'error_code': 'DB_OPERATION_ERROR', 'message': f'Errore database: {e}'}), 500
    except Exception as e_outer:
         logger.error(f"Errore generico imprevisto durante upload: {e_outer}", exc_info=True)
         if conn: conn.rollback()
         return jsonify({'success': False, 'error_code': 'UNEXPECTED_UPLOAD_ERROR', 'message': f'Errore server imprevisto: {e_outer}'}), 500
    finally:
        if conn:
            conn.close()
            logger.debug("Connessione DB chiusa.")

    # Risposta Finale
    overall_success = (total_processed > 0)
    final_message = f"Operazione upload completata. {len(processed_ok_info)} file indicizzati con successo."
    if processed_fail_info:
        final_message += f" {len(processed_fail_info)} file caricati ma con errori di indicizzazione."
    if skipped_files_info:
        final_message += f" {len(skipped_files_info)} file scartati prima dell'elaborazione."

    return jsonify({
        'success': overall_success,
        'message': final_message,
        'files_indexed_ok': processed_ok_info,
        'files_indexing_failed': processed_fail_info,
        'files_skipped': skipped_files_info
    }), 200 if overall_success else 400



@documents_bp.route('/<string:doc_id>', methods=['DELETE'])
@login_required
def delete_document(doc_id):
    """
    Elimina un documento specifico (record DB E CHUNKS CHROMA).
    Adattato per modalità single/saas e senza file fisici.
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

        # --- CONTROLLO ESISTENZA E APPARTENENZA (CORRETTO) ---
        sql_select = "SELECT doc_id FROM documents WHERE doc_id = ? AND user_id = ?"
        params_select = (doc_id, current_user_id)
        cursor.execute(sql_select, params_select)
        result = cursor.fetchone()

        if result is None:
            conn.close()
            message = f"Documento {doc_id} non trovato per l'utente {current_user_id}."
            logger.warning(message)
            return jsonify({'success': False, 'error_code': 'DOCUMENT_NOT_FOUND', 'message': message}), 404

        logger.info(f"[{doc_id}] Trovato documento appartenente all'utente. Procedo con l'eliminazione.")
        # --- FINE CONTROLLO ---

        # Elimina da ChromaDB (logica invariata)
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
                   chroma_delete_success = True # Se la collezione non esiste, è comunque un successo
        except Exception as e_chroma:
            logger.error(f"[{doc_id}] ERRORE durante l'eliminazione da ChromaDB: {e_chroma}", exc_info=True)

        # Elimina da SQLite
        sql_delete = "DELETE FROM documents WHERE doc_id = ? AND user_id = ?"
        cursor.execute(sql_delete, (doc_id, current_user_id))
        
        if cursor.rowcount == 0:
             conn.rollback(); conn.close()
             return jsonify({'success': False, 'error_code': 'DB_DELETE_FAILED', 'message': 'Errore DB durante eliminazione.'}), 500
        
        conn.commit()
        conn.close()

        final_message = f"Documento {doc_id} eliminato con successo dal database."
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