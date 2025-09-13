import logging
import sqlite3
import threading
import copy
from flask import Blueprint, jsonify, current_app
from flask_login import login_required, current_user
import os
import uuid
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
    logger.info(f"[_index_page][{page_id}] Avvio indicizzazione (Modalità: {app_mode}, UserID: {user_id})")

    final_status = 'failed_indexing'
    cursor = conn.cursor()

    try:
        # Recupera configurazione
        llm_api_key = current_app.config.get('GOOGLE_API_KEY')
        embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
        chunk_size = current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
        chunk_overlap = current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
        base_page_collection_name = "page_content" # Definiamo il nome base per la collezione

        # Ottieni/Crea la collezione ChromaDB per le pagine
        page_collection = None
        chroma_client = current_app.config.get('CHROMA_CLIENT')
        if not chroma_client:
            raise RuntimeError("Client ChromaDB non trovato nella configurazione.")
        
        collection_name = f"{base_page_collection_name}_{user_id}" if app_mode == 'saas' else base_page_collection_name
        page_collection = chroma_client.get_or_create_collection(name=collection_name)
        
        # Recupera dati della pagina dal DB
        cursor.execute("SELECT extracted_content_path, title, page_url FROM pages WHERE page_id = ?", (page_id,))
        page_data = cursor.fetchone()
        if not page_data:
            raise FileNotFoundError(f"Pagina con ID {page_id} non trovata nel database.")
        
        content_filepath, title, page_url = page_data

        with open(content_filepath, 'r', encoding='utf-8') as f:
            page_content = f.read()

        if not page_content.strip():
            logger.warning(f"[_index_page][{page_id}] File contenuto vuoto. Marco come completato.")
            final_status = 'completed'
        else:
            chunks = split_text_into_chunks(page_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            if not chunks:
                final_status = 'completed'
            else:
                embeddings = get_gemini_embeddings(chunks, api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_DOCUMENT)
                if not embeddings or len(embeddings) != len(chunks):
                    raise ValueError("Fallimento generazione embedding o numero non corrispondente.")
                
                ids = [f"{page_id}_chunk_{i}" for i in range(len(chunks))]
                metadatas = [{
                    "page_id": page_id, "page_title": title, "page_url": page_url,
                    "chunk_index": i, "source_type": "page",
                    **({"user_id": user_id} if app_mode == 'saas' and user_id else {})
                } for i in range(len(chunks))]
                
                page_collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=chunks)
                logger.info(f"[_index_page][{page_id}] Salvataggio di {len(chunks)} chunk in ChromaDB completato.")
                final_status = 'completed'

    except Exception as e:
        logger.error(f"[_index_page][{page_id}] Errore durante indicizzazione: {e}", exc_info=True)
        final_status = 'failed_indexing'
    
    # Aggiorna lo stato nel DB
    try:
        cursor.execute("UPDATE pages SET processing_status = ? WHERE page_id = ?", (final_status, page_id))
    except sqlite3.Error as db_err:
        logger.error(f"[_index_page][{page_id}] ERRORE CRITICO aggiornamento stato finale DB: {db_err}")
        return 'failed_db_update'

    return final_status

def _background_wp_sync_core(app_context, user_id: str, settings: dict):
    """
    Questa è la funzione che fa il lavoro pesante in un thread separato.
    """
    global wp_sync_status, wp_sync_lock
    
    with app_context:
        # Recupera le configurazioni necessarie dall'app context
        db_path = current_app.config.get('DATABASE_FILE')
        articles_folder = current_app.config.get('ARTICLES_FOLDER_PATH')
        # Creeremo una cartella separata per i contenuti delle pagine
        pages_folder = os.path.join(os.path.dirname(articles_folder), 'pages_content')
        os.makedirs(pages_folder, exist_ok=True)

        conn = None # Definiamo la connessione qui per usarla nel finally

        try:
            with wp_sync_lock:
                wp_sync_status.update({
                    'is_processing': True, 'message': 'Connessione a WordPress in corso...',
                    'error': None, 'total_items': 0, 'processed_items': 0
                })
            
            wp_client = WordPressClient(
                site_url=settings['wordpress_url'],
                username=settings['wordpress_username'],
                app_password=settings['wordpress_api_key']
            )
            
            posts = wp_client.get_all_posts()
            pages = wp_client.get_all_pages()
            
            all_items = [('post', item) for item in posts] + [('page', item) for item in pages]
            total_items_to_process = len(all_items)

            with wp_sync_lock:
                wp_sync_status['total_items'] = total_items_to_process

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            new_articles, updated_articles = 0, 0
            new_pages, updated_pages = 0, 0

            for idx, (item_type, item_data) in enumerate(all_items):
                processed_count = idx + 1
                title_obj = item_data.get('title', {})
                title_text = title_obj.get('rendered', 'Senza Titolo')
                item_url = item_data.get('link')
                
                # Pulisce il contenuto HTML
                content_html = item_data.get('content', {}).get('rendered', '')
                soup = BeautifulSoup(content_html, 'html.parser')
                content_text = soup.get_text(separator='\n', strip=True)

                if not item_url or not content_text:
                    logger.warning(f"Salto item '{title_text}' perché manca URL o contenuto.")
                    continue

                if item_type == 'post':
                    # --- GESTIONE ARTICOLI ---
                    with wp_sync_lock:
                        wp_sync_status['message'] = f"Recupero e indicizzazione articolo ({processed_count}/{total_items_to_process}): {title_text[:30]}..."
                        wp_sync_status['processed_items'] = processed_count
                    
                    cursor.execute("SELECT article_id, content_hash FROM articles WHERE article_url = ? AND user_id = ?", (item_url, user_id))
                    existing_article = cursor.fetchone()

                    content_hash = str(hash(content_text))

                    if existing_article:
                        # Articolo già esistente, controlla se il contenuto è cambiato
                        if existing_article[1] != content_hash:
                            # Contenuto diverso, aggiorna e re-indicizza
                            article_id = existing_article[0]
                            content_path = os.path.join(articles_folder, f"{article_id}.txt")
                            with open(content_path, 'w', encoding='utf-8') as f:
                                f.write(content_text)
                            cursor.execute("UPDATE articles SET content_hash = ?, processing_status = 'pending' WHERE article_id = ?", (content_hash, article_id))
                            _index_article(article_id, conn, user_id)
                            updated_articles += 1
                    else:
                        # Nuovo articolo
                        article_id = str(uuid.uuid4())
                        content_path = os.path.join(articles_folder, f"{article_id}.txt")
                        with open(content_path, 'w', encoding='utf-8') as f:
                            f.write(content_text)
                        
                        published_date = item_data.get('modified_gmt', '') + 'Z'
                        cursor.execute("""
                            INSERT INTO articles (article_id, article_url, title, published_at, extracted_content_path, content_hash, user_id, processing_status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                        """, (article_id, item_url, title_text, published_date, content_path, content_hash, user_id))
                        _index_article(article_id, conn, user_id)
                        new_articles += 1
                
                elif item_type == 'page':
                    # --- GESTIONE PAGINE ---
                    with wp_sync_lock:
                        wp_sync_status['message'] = f"Recupero e indicizzazione pagina ({processed_count}/{total_items_to_process}): {title_text[:30]}..."
                        wp_sync_status['processed_items'] = processed_count

                    cursor.execute("SELECT page_id, content_hash FROM pages WHERE page_url = ? AND user_id = ?", (item_url, user_id))
                    existing_page = cursor.fetchone()

                    content_hash = str(hash(content_text))

                    if existing_page:
                        if existing_page[1] != content_hash:
                            page_id = existing_page[0]
                            content_path = os.path.join(pages_folder, f"{page_id}.txt")
                            with open(content_path, 'w', encoding='utf-8') as f:
                                f.write(content_text)
                            cursor.execute("UPDATE pages SET content_hash = ?, processing_status = 'pending' WHERE page_id = ?", (content_hash, page_id))
                            _index_page(page_id, conn, user_id)
                            updated_pages += 1
                    else:
                        page_id = str(uuid.uuid4())
                        content_path = os.path.join(pages_folder, f"{page_id}.txt")
                        with open(content_path, 'w', encoding='utf-8') as f:
                            f.write(content_text)
                        
                        published_date = item_data.get('modified_gmt', '') + 'Z'
                        cursor.execute("""
                            INSERT INTO pages (page_id, page_url, title, published_at, extracted_content_path, content_hash, user_id, processing_status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                        """, (page_id, item_url, title_text, published_date, content_path, content_hash, user_id))
                        _index_page(page_id, conn, user_id)
                        new_pages += 1
                
                conn.commit() # Commit dopo ogni item per salvare i progressi

            final_message = f"Sincronizzazione completata! Articoli: {new_articles} nuovi, {updated_articles} aggiornati. Pagine: {new_pages} nuove, {updated_pages} aggiornate."
            logger.info(final_message)
            
            with wp_sync_lock:
                wp_sync_status.update({'is_processing': False, 'message': final_message})

        except Exception as e:
            logger.error(f"Errore nel thread di sincronizzazione WordPress per l'utente {user_id}: {e}", exc_info=True)
            error_message = f"Errore: {e}"
            if '401' in str(e) or '403' in str(e):
                 error_message = 'Errore di autenticazione. Controlla le credenziali.'
            
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