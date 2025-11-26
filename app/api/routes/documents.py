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

def _index_document(doc_id: str, conn: sqlite3.Connection, user_id: str, core_config: dict) -> str:
    """
    Esegue l'indicizzazione di un documento (lettura MD, chunk, embed, Chroma).
    NON fa commit; si aspetta che il chiamante gestisca la transazione.
    Restituisce lo stato finale ('completed' o 'failed_...').
    """
    logger.info(f"[_index_document][{doc_id}] Avvio indicizzazione per UserID: {user_id}")
    final_status = 'failed_indexing_init'
    cursor = conn.cursor()

    embedding_model = core_config.get('GEMINI_EMBEDDING_MODEL')
    chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
    chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
    base_doc_collection_name = core_config.get('DOCUMENT_COLLECTION_NAME', 'document_content')
    use_agentic_chunking = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'

    chroma_client = core_config.get('CHROMA_CLIENT')
    if not chroma_client: 
        logger.error(f"[_index_document][{doc_id}] Chroma Client non trovato!")
        return 'failed_config_client_missing'
    if not user_id:
        logger.error(f"[_index_document][{doc_id}] User ID mancante!")
        return 'failed_user_id_missing'

    user_doc_collection_name = f"{base_doc_collection_name}_{user_id}"
    try:
        doc_collection = chroma_client.get_or_create_collection(name=user_doc_collection_name)
    except Exception as e_coll:
        logger.error(f"[_index_document][{doc_id}] Errore get/create collezione '{user_doc_collection_name}': {e_coll}")
        return 'failed_chroma_collection'

    logger.info(f"[_index_document][{doc_id}] Collezione Chroma '{user_doc_collection_name}' pronta.")

    try:
        cursor.execute("SELECT content, original_filename FROM documents WHERE doc_id = ?", (doc_id,))
        doc_data = cursor.fetchone()
        if not doc_data: 
            logger.error(f"[_index_document][{doc_id}] Record non trovato nel DB.")
            return 'failed_doc_not_found'
        
        markdown_content, original_filename = doc_data[0], doc_data[1]
        
        if not markdown_content or not markdown_content.strip():
             final_status = 'completed'
        else:
             chunks = []
             if use_agentic_chunking:
                try:
                    chunks = chunk_text_agentically(
                        markdown_content, 
                        llm_provider=core_config.get('llm_provider', 'google'), 
                        settings=core_config
                    )
                except google_exceptions.ResourceExhausted as e:
                    logger.warning(f"[_index_document][{doc_id}] Quota API esaurita. Fallback a chunking classico. Errore: {e}")
                    chunks = [] 

                if not chunks:
                    logger.warning(f"[_index_document][{doc_id}] Chunking intelligente fallito. Fallback a classico.")
                    chunks = split_text_into_chunks(markdown_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
             else:
                chunks = split_text_into_chunks(markdown_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

             if not chunks:
                 final_status = 'completed'
             else:
                 embeddings = generate_embeddings(chunks, user_settings=core_config, task_type=TASK_TYPE_DOCUMENT)
                 if not embeddings or len(embeddings) != len(chunks):
                     final_status = 'failed_embedding'
                 else:
                     ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
                     metadatas_chroma = [{
                         "doc_id": doc_id, "original_filename": original_filename,
                         "chunk_index": i, "source_type": "document",
                         "user_id": user_id
                     } for i in range(len(chunks))]
                     doc_collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas_chroma, documents=chunks)
                     final_status = 'completed'

    except Exception as e:
        logger.error(f"[_index_document][{doc_id}] Errore inatteso: {e}", exc_info=True)
        final_status = 'failed_processing_generic'

    if final_status == 'completed':
        try:
            import textstat
            stats = { 'word_count': 0, 'gunning_fog': 0 }
            if markdown_content and markdown_content.strip():
                stats['word_count'] = len(markdown_content.split())
                stats['gunning_fog'] = textstat.gunning_fog(markdown_content)

            cursor.execute("""
                INSERT INTO content_stats (content_id, user_id, source_type, word_count, gunning_fog)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET
                    word_count = excluded.word_count,
                    gunning_fog = excluded.gunning_fog,
                    last_calculated = CURRENT_TIMESTAMP
            """, (doc_id, user_id, 'document', stats['word_count'], stats['gunning_fog']))
        except Exception as e_stats:
            logger.error(f"[_index_document][{doc_id}] Errore calcolo statistiche: {e_stats}")

    try:
        cursor.execute("UPDATE documents SET processing_status = ? WHERE doc_id = ?", (final_status, doc_id))
    except sqlite3.Error as db_update_err:
         logger.error(f"[_index_document][{doc_id}] Errore CRITICO aggiornamento DB: {db_update_err}")
         final_status = 'failed_db_status_update'

    return final_status


@documents_bp.route('/upload', methods=['POST'])
@login_required
def upload_documents():
    """
    Riceve file, estrae testo, salva, registra nel DB con user_id
    e avvia l'indicizzazione automaticamente.
    """
    current_user_id = current_user.id
    logger.info(f"Richiesta upload documenti per utente '{current_user_id}'")

    upload_folder = current_app.config.get('UPLOAD_FOLDER_PATH')
    db_path = current_app.config.get('DATABASE_FILE')
    if not upload_folder or not db_path:
        return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Errore configurazione server.'}), 500
    os.makedirs(upload_folder, exist_ok=True)

    if 'documents' not in request.files:
        return jsonify({'success': False, 'error_code': 'NO_FILE_PART', 'message': "Nessuna parte 'documents' nella richiesta."}), 400

    uploaded_files = request.files.getlist('documents')
    if not uploaded_files or all(f.filename == '' for f in uploaded_files):
         return jsonify({'success': False, 'error_code': 'NO_FILE_SELECTED', 'message': 'Nessun file selezionato.'}), 400

    processed_ok_info, processed_fail_info, skipped_files_info = [], [], []
    conn = None

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        core_config_dict = build_full_config_for_background_process(current_user_id)

        for file in uploaded_files:
            original_filename = secure_filename(file.filename) if file and file.filename else 'N/A'

            if file and file.filename and allowed_file(original_filename):
                file.seek(0, os.SEEK_END)
                file_size = file.tell()
                file.seek(0)
                extracted_text = extract_text_from_file(file, original_filename)
                content_size = len(extracted_text.encode('utf-8')) if extracted_text else 0

                if extracted_text is not None:
                    doc_id = str(uuid.uuid4())
                    try:
                        # --- content_size alla query e ai valori ---
                        cursor.execute(
                            "INSERT INTO documents (doc_id, original_filename, content, mimetype, user_id, processing_status, filesize, content_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (doc_id, original_filename, extracted_text, file.mimetype, current_user_id, 'pending', file_size, content_size)
                        )
                        indexing_status = _index_document(doc_id, conn, current_user_id, core_config_dict)

                        if indexing_status == 'completed':
                            processed_ok_info.append({ 'doc_id': doc_id, 'original_filename': original_filename })
                        else:
                            processed_fail_info.append({ 'doc_id': doc_id, 'original_filename': original_filename, 'final_status': indexing_status })
                    except Exception as process_err:
                         skipped_files_info.append({'filename': original_filename, 'error': f'Errore: {process_err}'})
                else:
                     skipped_files_info.append({'filename': original_filename, 'error': 'Estrazione testo fallita.'})
            elif file and file.filename:
                skipped_files_info.append({'filename': original_filename, 'error': 'Tipo file non permesso'})
        
        conn.commit()

    except Exception as e_outer:
         if conn: conn.rollback()
         return jsonify({'success': False, 'error_code': 'UNEXPECTED_UPLOAD_ERROR', 'message': f'Errore server: {e_outer}'}), 500
    finally:
        if conn: conn.close()

    overall_success = len(processed_ok_info) > 0 or (len(processed_fail_info) == 0 and len(skipped_files_info) == 0)
    final_message = f"Operazione completata. {len(processed_ok_info)} indicizzati, {len(processed_fail_info)} falliti, {len(skipped_files_info)} scartati."
    return jsonify({
        'success': overall_success, 'message': final_message, 'files_indexed_ok': processed_ok_info,
        'files_indexing_failed': processed_fail_info, 'files_skipped': skipped_files_info
    }), 200 if overall_success else 400


@documents_bp.route('/<string:doc_id>', methods=['DELETE'])
@login_required
def delete_document(doc_id):
    """
    Elimina un documento specifico (record DB, chunks Chroma e statistiche),
    verificando che appartenga all'utente loggato.
    """
    logger.info(f"Richiesta DELETE per doc_id: {doc_id}")
    current_user_id = current_user.id

    db_path = current_app.config.get('DATABASE_FILE')
    base_doc_collection_name = current_app.config.get('DOCUMENT_COLLECTION_NAME', 'document_content')

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT doc_id FROM documents WHERE doc_id = ? AND user_id = ?", (doc_id, current_user_id))
        if cursor.fetchone() is None:
            return jsonify({'success': False, 'error_code': 'DOCUMENT_NOT_FOUND', 'message': "Documento non trovato o non autorizzato."}), 404

        chroma_client = current_app.config.get('CHROMA_CLIENT')
        if chroma_client:
            user_doc_collection_name = f"{base_doc_collection_name}_{current_user_id}"
            try:
                doc_collection = chroma_client.get_collection(name=user_doc_collection_name)
                chunks_to_delete = doc_collection.get(where={"doc_id": doc_id}, include=[])
                if chunks_to_delete.get('ids'):
                    doc_collection.delete(ids=chunks_to_delete['ids'])
            except Exception:
                pass 

        cursor.execute("DELETE FROM documents WHERE doc_id = ? AND user_id = ?", (doc_id, current_user_id))
        cursor.execute("DELETE FROM content_stats WHERE content_id = ? AND user_id = ?", (doc_id, current_user_id))
        
        conn.commit()

        return jsonify({'success': True, 'message': f"Documento {doc_id} eliminato."}), 200

    except Exception as e:
         if conn: conn.rollback()
         return jsonify({'success': False, 'error_code': 'UNEXPECTED_DELETE_ERROR', 'message': 'Errore server.'}), 500
    finally:
         if conn: conn.close()