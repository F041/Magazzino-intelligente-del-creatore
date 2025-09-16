import logging
import sqlite3
import threading
import copy
from flask import Blueprint, jsonify, current_app
from flask_login import login_required, current_user
import os
import uuid
import markdownify as md # ho provato ad importare per preservare link nelle pagine, per favorire i contatti, ma ho fallito
from bs4 import BeautifulSoup # per estrarre solo il testo pulito dal contenuto HTML fornito da WordPress
from typing import Optional 
from app.services.embedding.gemini_embedding import split_text_into_chunks, get_gemini_embeddings, TASK_TYPE_DOCUMENT


# Importiamo il nostro client WordPress
from app.services.wordpress.client import WordPressClient
from .rss import _index_article

logger = logging.getLogger(__name__)
connectors_bp = Blueprint('connectors', __name__)

# --- STATO GLOBALE e LOCK per il Processo di Sincronizzazione WordPress ---
wp_sync_status = {
    'is_processing': False,
    'total_items': 0,
    'processed_items': 0,
    'message': '',
    'error': None
}
wp_sync_lock = threading.Lock()


def _index_page(page_id: str, conn: sqlite3.Connection, user_id: Optional[str] = None) -> str:
    """
    Esegue l'indicizzazione di una pagina (legge contenuto, chunk, embed, Chroma).
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_index_page][{page_id}] Avvio indicizzazione reale (Modalità: {app_mode}, UserID: {user_id})")

    final_status = 'failed_indexing'
    cursor = conn.cursor()

    try:
        # Recupera configurazione
        llm_api_key = current_app.config.get('GOOGLE_API_KEY')
        embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
        chunk_size = current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
        chunk_overlap = current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
        base_page_collection_name = "page_content"

        chroma_client = current_app.config.get('CHROMA_CLIENT')
        if not chroma_client:
            raise RuntimeError("Client ChromaDB non trovato.")
        
        collection_name = f"{base_page_collection_name}_{user_id}" if app_mode == 'saas' else base_page_collection_name
        page_collection = chroma_client.get_or_create_collection(name=collection_name)
        
        cursor.execute("SELECT extracted_content_path, title, page_url FROM pages WHERE page_id = ?", (page_id,))
        page_data = cursor.fetchone()
        if not page_data:
            raise FileNotFoundError(f"Pagina con ID {page_id} non trovata nel DB.")
        
        content_filepath, title, page_url = page_data[0], page_data[1], page_data[2]

        with open(content_filepath, 'r', encoding='utf-8') as f:
            page_content = f.read()

        if not page_content.strip():
            final_status = 'completed'
        else:
            chunks = split_text_into_chunks(page_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            if not chunks:
                final_status = 'completed'
            else:
                embeddings = get_gemini_embeddings(chunks, api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_DOCUMENT)
                if not embeddings or len(embeddings) != len(chunks):
                    raise ValueError("Fallimento generazione embedding.")
                
                ids = [f"{page_id}_chunk_{i}" for i in range(len(chunks))]
                metadatas = [{
                    "page_id": page_id, "page_title": title, "page_url": page_url,
                    "chunk_index": i, "source_type": "page",
                    **({"user_id": user_id} if app_mode == 'saas' and user_id else {})
                } for i in range(len(chunks))]
                
                page_collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=chunks)
                final_status = 'completed'

    except Exception as e:
        logger.error(f"[_index_page][{page_id}] Errore durante indicizzazione: {e}", exc_info=True)
        final_status = 'failed_indexing'
    
    try:
        cursor.execute("UPDATE pages SET processing_status = ? WHERE page_id = ?", (final_status, page_id))
    except sqlite3.Error as db_err:
        logger.error(f"[_index_page][{page_id}] Errore DB: {db_err}")
        return 'failed_db_update'

    return final_status


def _delete_page_permanently(page_id: str, conn: sqlite3.Connection, user_id: Optional[str] = None):
    """
    Elimina completamente una pagina: record DB, file di testo e chunk da ChromaDB.
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_delete_page][{page_id}] Avvio eliminazione permanente (UserID: {user_id}).")
    cursor = conn.cursor()

    try:
        # 1. Recupera il percorso del file prima di cancellare il record DB
        cursor.execute("SELECT extracted_content_path FROM pages WHERE page_id = ? AND user_id = ?", (page_id, user_id))
        result = cursor.fetchone()
        if not result:
            logger.warning(f"[_delete_page][{page_id}] Pagina non trovata nel DB, impossibile eliminare.")
            return False
        
        filepath_to_delete = result[0]

        # 2. Elimina da ChromaDB
        try:
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            base_page_collection_name = "page_content"
            collection_name = f"{base_page_collection_name}_{user_id}" if app_mode == 'saas' else base_page_collection_name
            page_collection = chroma_client.get_collection(name=collection_name)
            
            # Trova gli ID dei chunk da eliminare
            chunks_to_delete = page_collection.get(where={"page_id": page_id})
            chunk_ids = chunks_to_delete.get('ids', [])
            if chunk_ids:
                page_collection.delete(ids=chunk_ids)
                logger.info(f"[_delete_page][{page_id}] Eliminati {len(chunk_ids)} chunk da ChromaDB.")
        except Exception as e:
            # Se la collezione non esiste o c'è un errore, logghiamo ma non blocchiamo il processo
            logger.error(f"[_delete_page][{page_id}] Errore durante eliminazione da ChromaDB (procedo comunque): {e}")

        # 3. Elimina il record dal database SQLite
        cursor.execute("DELETE FROM pages WHERE page_id = ? AND user_id = ?", (page_id, user_id))
        if cursor.rowcount == 0:
            logger.warning(f"[_delete_page][{page_id}] Nessuna riga eliminata dal DB (potrebbe essere già stata cancellata).")
        
        # 4. Elimina il file fisico
        if filepath_to_delete and os.path.exists(filepath_to_delete):
            os.remove(filepath_to_delete)
            logger.info(f"[_delete_page][{page_id}] File fisico {filepath_to_delete} eliminato.")

        # Non facciamo commit qui, sarà gestito dal chiamante (la funzione di sync)
        return True

    except Exception as e:
        logger.error(f"[_delete_page][{page_id}] Errore imprevisto durante l'eliminazione: {e}", exc_info=True)
        return False

def _background_wp_sync_core(app_context, user_id: str, settings: dict):
    """
    Esegue il lavoro pesante: scarica, confronta, aggiorna, aggiunge E CANCELLA i contenuti di WordPress.
    """
    global wp_sync_status, wp_sync_lock
    
    with app_context:
        db_path = current_app.config.get('DATABASE_FILE')
        articles_folder = current_app.config.get('ARTICLES_FOLDER_PATH')
        pages_folder = os.path.join(os.path.dirname(articles_folder), 'page_content')
        os.makedirs(pages_folder, exist_ok=True)
        conn = None

        try:
            with wp_sync_lock:
                wp_sync_status.update({'is_processing': True, 'message': 'Connessione a WordPress...', 'error': None, 'total_items': 0, 'processed_items': 0})
            
            wp_client = WordPressClient(
                site_url=settings['wordpress_url'],
                username=settings['wordpress_username'],
                app_password=settings['wordpress_api_key']
            )
            
            # --- FASE 1: RECUPERO DATI ---
            with wp_sync_lock: wp_sync_status['message'] = 'Recupero articoli e pagine dal sito...'
            posts_from_wp = wp_client.get_all_posts()
            pages_from_wp = wp_client.get_all_pages()
            
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # --- FASE 2: CONFRONTO E PULIZIA CONTENUTI ORFANI ---
            # Pagine
            cursor.execute("SELECT page_id, page_url FROM pages WHERE user_id = ?", (user_id,))
            pages_in_db = {row[1]: row[0] for row in cursor.fetchall()} # {url: page_id}
            urls_from_wp_pages = {page['link'] for page in pages_from_wp}
            pages_to_delete_urls = set(pages_in_db.keys()) - urls_from_wp_pages
            
            deleted_pages_count = 0
            if pages_to_delete_urls:
                logger.info(f"Trovate {len(pages_to_delete_urls)} pagine da eliminare.")
                for url in pages_to_delete_urls:
                    page_id_to_delete = pages_in_db[url]
                    with wp_sync_lock: wp_sync_status['message'] = f'Eliminazione pagina obsoleta: {url[:50]}...'
                    if _delete_page_permanently(page_id_to_delete, conn, user_id):
                        deleted_pages_count += 1
                conn.commit() # Salva le eliminazioni
            
            # Articoli
            cursor.execute("SELECT article_id, article_url FROM articles WHERE user_id = ?", (user_id,))
            articles_in_db = {row[1]: row[0] for row in cursor.fetchall()}
            urls_from_wp_posts = {post['link'] for post in posts_from_wp}
            articles_to_delete_urls = set(articles_in_db.keys()) - urls_from_wp_posts
            
            deleted_articles_count = 0
            if articles_to_delete_urls:
                logger.info(f"Trovati {len(articles_to_delete_urls)} articoli da eliminare.")
                # Per eliminare gli articoli, avremmo bisogno di una funzione _delete_article_permanently
                # Per ora, ci limitiamo a loggare il risultato per non introdurre nuovo codice non richiesto.
                # In una prossima iterazione, potremo implementare anche questa pulizia.
                logger.warning(f"La pulizia degli articoli obsoleti non è ancora implementata. Trovati {len(articles_to_delete_urls)} articoli da rimuovere.")

            # --- FASE 3: AGGIUNTA E AGGIORNAMENTO ---
            all_items = [('post', item) for item in posts_from_wp] + [('page', item) for item in pages_from_wp]
            total_items_to_process = len(all_items)
            with wp_sync_lock: wp_sync_status['total_items'] = total_items_to_process

            new_articles, updated_articles, new_pages, updated_pages = 0, 0, 0, 0
            
            for idx, (item_type, item_data) in enumerate(all_items):
                processed_count = idx + 1
                title_obj = item_data.get('title', {})
                title_text = title_obj.get('rendered', 'Senza Titolo')
                item_url = item_data.get('link')
                content_html = item_data.get('content', {}).get('rendered', '')
                soup = BeautifulSoup(content_html, 'html.parser')
                content_text = soup.get_text(separator='\n', strip=True)

                if not item_url or not content_text:
                    continue

                type_str_log = "articolo" if item_type == 'post' else "pagina"
                with wp_sync_lock:
                    wp_sync_status['message'] = f"Recupero e indicizzazione {type_str_log} ({processed_count}/{total_items_to_process}): {title_text[:30]}..."
                    wp_sync_status['processed_items'] = processed_count

                content_hash = str(hash(content_text))

                if item_type == 'post':
                    # Determina l'ID univoco e la data di pubblicazione
                    published_date = item_data.get('modified_gmt', '') + 'Z'
                    guid = item_data.get('guid', {}).get('rendered', item_url)

                    # Determina l'ID dell'articolo e il percorso del file.
                    # Prima controlliamo se esiste già per riutilizzare l'ID e il percorso.
                    cursor.execute("SELECT article_id, extracted_content_path FROM articles WHERE article_url = ? AND user_id = ?", (item_url, user_id))
                    existing_article = cursor.fetchone()
                    
                    if existing_article:
                        article_id = existing_article[0]
                        content_path = existing_article[1]
                    else:
                        article_id = str(uuid.uuid4())
                        content_path = os.path.join(articles_folder, f"{article_id}.txt")

                    # Scrivi (o sovrascrivi) il contenuto del file
                    with open(content_path, 'w', encoding='utf-8') as f:
                        f.write(content_text)

                    # Esegui un'unica operazione SQL robusta:
                    # Inserisce una nuova riga. Se l'URL esiste già (conflitto),
                    # aggiorna la riga esistente solo se il contenuto è cambiato.
                    cursor.execute("""
                        INSERT INTO articles (article_id, article_url, title, guid, published_at, extracted_content_path, content_hash, user_id, processing_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                        ON CONFLICT(article_url) DO UPDATE SET
                            title = excluded.title,
                            published_at = excluded.published_at,
                            extracted_content_path = excluded.extracted_content_path,
                            content_hash = excluded.content_hash,
                            processing_status = 'pending'
                        WHERE content_hash IS NOT excluded.content_hash;
                    """, (article_id, item_url, title_text, guid, published_date, content_path, content_hash, user_id))

                    # Se la query ha modificato qualcosa (INSERT o UPDATE), ri-indicizziamo.
                    if cursor.rowcount > 0:
                        _index_article(article_id, conn, user_id)
                        # Semplifichiamo il conteggio: ogni articolo processato viene contato.
                        # Potremmo distinguere tra 'nuovo' e 'aggiornato' in futuro se necessario.
                        new_articles += 1
                
                elif item_type == 'page':
                    cursor.execute("SELECT page_id, content_hash FROM pages WHERE page_url = ? AND user_id = ?", (item_url, user_id))
                    existing = cursor.fetchone()
                    if existing:
                        if existing[1] != content_hash:
                            item_id = existing[0]
                            content_path = os.path.join(pages_folder, f"{item_id}.txt")
                            with open(content_path, 'w', encoding='utf-8') as f: f.write(content_text)
                            cursor.execute("UPDATE pages SET content_hash = ?, processing_status = 'pending' WHERE page_id = ?", (content_hash, item_id))
                            _index_page(item_id, conn, user_id)
                            updated_pages += 1
                    else:
                        item_id = str(uuid.uuid4())
                        content_path = os.path.join(pages_folder, f"{item_id}.txt")
                        with open(content_path, 'w', encoding='utf-8') as f: f.write(content_text)
                        published_date = item_data.get('modified_gmt', '') + 'Z'
                        cursor.execute("INSERT INTO pages (page_id, page_url, title, published_at, extracted_content_path, content_hash, user_id, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                                       (item_id, item_url, title_text, published_date, content_path, content_hash, user_id))
                        _index_page(item_id, conn, user_id)
                        new_pages += 1
                
                conn.commit()

            final_message = f"Sincronizzazione completata! Articoli: {new_articles} nuovi, {updated_articles} aggiornati. Pagine: {new_pages} nuove, {updated_pages} aggiornate, {deleted_pages_count} eliminate."
            with wp_sync_lock:
                wp_sync_status.update({'is_processing': False, 'message': final_message})

        except Exception as e:
            error_message = f"Errore: {e}"
            if '401' in str(e) or '403' in str(e): error_message = 'Errore di autenticazione. Controlla le credenziali.'
            with wp_sync_lock:
                wp_sync_status.update({'is_processing': False, 'error': error_message, 'message': "Sincronizzazione fallita."})
        finally:
            if conn:
                conn.close()

@connectors_bp.route('/wordpress/sync', methods=['POST'])
@login_required
def sync_wordpress():
    """
    AVVIA la sincronizzazione dei contenuti da un sito WordPress in background.
    """
    global wp_sync_status, wp_sync_lock
    user_id = current_user.id
    
    with wp_sync_lock:
        if wp_sync_status['is_processing']:
            return jsonify({'success': False, 'message': 'Una sincronizzazione WordPress è già in corso.'}), 409

    # Recupera le impostazioni
    db_path = current_app.config.get('DATABASE_FILE')
    settings_row = None # Rinominiamo la variabile per chiarezza
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT wordpress_url, wordpress_username, wordpress_api_key FROM user_settings WHERE user_id = ?", (user_id,))
        settings_row = cursor.fetchone()
    finally:
        if conn: conn.close()
    
    # --- CORREZIONE CHIAVE QUI ---
    # Convertiamo l'oggetto sqlite3.Row in un vero dizionario
    settings_dict = dict(settings_row) if settings_row else None
    
    # Ora controlliamo il dizionario, che ha il metodo .values()
    if not settings_dict or not all(settings_dict.values()):
        return jsonify({'success': False, 'message': 'Configurazione WordPress incompleta. Controlla URL, nome utente e Application Password nelle Impostazioni.'}), 400

    # Avvia il thread in background
    app_context = current_app.app_context()
    background_thread = threading.Thread(
        target=_background_wp_sync_core,
        args=(app_context, user_id, settings_dict) # Passiamo il dizionario
    )
    background_thread.daemon = True
    background_thread.start()
    
    logger.info(f"Avviato thread di sincronizzazione WordPress per l'utente: {user_id}")
    return jsonify({'success': True, 'message': 'Sincronizzazione avviata in background.'}), 202

@connectors_bp.route('/wordpress/progress', methods=['GET'])
@login_required
def get_wordpress_sync_progress():
    """
    Restituisce lo stato attuale del processo di sincronizzazione WordPress.
    """
    global wp_sync_status, wp_sync_lock
    with wp_sync_lock:
        return jsonify(copy.deepcopy(wp_sync_status))
    
@connectors_bp.route('/pages/all', methods=['DELETE'])
@login_required
def delete_all_user_pages():
    """
    Elimina tutte le pagine per l'utente corrente (SAAS mode).
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    if app_mode != 'saas':
        return jsonify({'success': False, 'message': 'Operazione permessa solo in modalità SAAS.'}), 403
    user_id = current_user.id
    logger.info(f"Avvio eliminazione di massa delle pagine per l'utente: {user_id}")

    conn = None
    deleted_count = 0
    try:
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Prima recupera tutti gli ID delle pagine per poter chiamare la funzione di pulizia
        cursor.execute("SELECT page_id FROM pages WHERE user_id = ?", (user_id,))
        page_ids_to_delete = [row[0] for row in cursor.fetchall()]
        
        if not page_ids_to_delete:
            return jsonify({'success': True, 'message': 'Nessuna pagina da eliminare.'})

        for page_id in page_ids_to_delete:
            if _delete_page_permanently(page_id, conn, user_id):
                deleted_count += 1
        
        conn.commit() # Salva tutte le modifiche fatte da _delete_page_permanently
        
        message = f"Eliminazione completata. Rimosse {deleted_count} pagine."
        logger.info(message)
        return jsonify({'success': True, 'message': message})

    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"Errore durante l'eliminazione di massa delle pagine per l'utente {user_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Si è verificato un errore sul server durante l\'eliminazione.'}), 500
    finally:
        if conn:
            conn.close()

@connectors_bp.route('/pages/<string:page_id>', methods=['DELETE'])
@login_required
def delete_single_page(page_id):
    """
    Elimina una singola pagina per l'utente corrente (SAAS mode).
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    if app_mode != 'saas':
        return jsonify({'success': False, 'message': 'Operazione permessa solo in modalità SAAS.'}), 403
    
    user_id = current_user.id
    logger.info(f"Richiesta eliminazione singola pagina ID: {page_id} per utente: {user_id}")

    conn = None
    try:
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        
        # Usiamo la nostra funzione helper che fa tutto il lavoro sporco
        success = _delete_page_permanently(page_id, conn, user_id)
        
        if success:
            conn.commit()
            message = "Pagina eliminata con successo."
            logger.info(message)
            return jsonify({'success': True, 'message': message})
        else:
            # La funzione helper logga già l'errore, qui restituiamo un messaggio generico
            conn.rollback()
            message = "Impossibile eliminare la pagina. Potrebbe non essere stata trovata."
            logger.warning(f"Tentativo di eliminazione fallito per la pagina {page_id} dell'utente {user_id}.")
            return jsonify({'success': False, 'message': message}), 404

    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"Errore grave durante l'eliminazione della pagina {page_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Si è verificato un errore sul server.'}), 500
    finally:
        if conn:
            conn.close()

@connectors_bp.route('/pages/download_all', methods=['GET'])
@login_required
def download_all_pages():
    app_mode = current_app.config.get('APP_MODE', 'single')
    db_path = current_app.config.get('DATABASE_FILE')
    logger.info(f"Richiesta download contenuto pagine (Modalità: {app_mode})")

    current_user_id = current_user.id if app_mode == 'saas' else None

    if not db_path:
        return jsonify({'success': False, 'error': 'Server configuration error.'}), 500

    all_pages_content = __import__('io').StringIO()
    conn = None
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        sql_query = "SELECT page_id, title, page_url, extracted_content_path FROM pages WHERE processing_status = 'completed' AND extracted_content_path IS NOT NULL"
        params = []
        if app_mode == 'saas':
           sql_query += " AND user_id = ?"
           params.append(current_user_id)
        
        cursor.execute(sql_query, tuple(params))

        for row in cursor.fetchall():
            content_path = row['extracted_content_path']
            if content_path and os.path.exists(content_path):
                with open(content_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                all_pages_content.write(f"--- PAGINA START ---\n")
                all_pages_content.write(f"ID: {row['page_id']}\n")
                all_pages_content.write(f"Titolo: {row['title']}\n")
                all_pages_content.write(f"URL: {row['page_url']}\n")
                all_pages_content.write(f"--- Contenuto ---\n{content}\n")
                all_pages_content.write(f"--- PAGINA END ---\n\n\n")

        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Errore DB durante recupero pagine per download: {e}")
        if conn: conn.close()
        return jsonify({'success': False, 'error': 'Database error.'}), 500

    output_filename = f"pages_{current_user_id if app_mode=='saas' else 'all'}.txt"
    file_content = all_pages_content.getvalue()
    all_pages_content.close()

    return __import__('flask').Response(
        file_content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment;filename={output_filename}"}
    )