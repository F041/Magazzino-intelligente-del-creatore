import logging
import sqlite3
import threading
import copy
from flask import Blueprint, jsonify, current_app
from flask_login import login_required, current_user
import os
import textstat
import hashlib
import uuid
import html
import markdownify as md
from bs4 import BeautifulSoup
from typing import Optional 
from app.services.embedding.gemini_embedding import split_text_into_chunks, get_gemini_embeddings, TASK_TYPE_DOCUMENT
from app.utils import build_full_config_for_background_process, normalize_url
from app.services.wordpress.client import WordPressClient
from .rss import _index_article

logger = logging.getLogger(__name__)
connectors_bp = Blueprint('connectors', __name__)

# Questa variabile globale terrà traccia dello stato del processo in background.
wp_sync_status = {
    'is_processing': False, 'total_items': 0, 'processed_items': 0,
    'message': '', 'error': None, 'new_items': 0, 'updated_items': 0,
    'skipped_items': 0, 'deleted_items': 0
}
wp_sync_lock = threading.Lock() # Un "semaforo" per evitare che più processi scrivano qui contemporaneamente

def _index_page(page_id: str, conn: sqlite3.Connection, user_id: Optional[str] = None, core_config: Optional[dict] = None) -> str:
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_index_page][{page_id}] Avvio indicizzazione reale (Modalità: {app_mode}, UserID: {user_id})")

    final_status = 'failed_indexing'
    cursor = conn.cursor()

    try:
        config = core_config or current_app.config
        llm_api_key = config.get('GOOGLE_API_KEY')
        embedding_model = config.get('GEMINI_EMBEDDING_MODEL')
        chunk_size = config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
        chunk_overlap = config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
        base_page_collection_name = "page_content"
        chroma_client = config.get('CHROMA_CLIENT')
        if not chroma_client:
            raise RuntimeError("Client ChromaDB non trovato.")
        
        collection_name = f"{base_page_collection_name}_{user_id}" if app_mode == 'saas' else base_page_collection_name
        page_collection = chroma_client.get_or_create_collection(name=collection_name)
        
        cursor.execute("SELECT content, title, page_url FROM pages WHERE page_id = ?", (page_id,))
        page_data = cursor.fetchone()
        if not page_data:
            raise FileNotFoundError(f"Pagina con ID {page_id} non trovata nel DB.")
        
        page_content, title, page_url = page_data[0], page_data[1], page_data[2]

        if not page_content or not page_content.strip():
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

    if final_status == 'completed':
        try:
            stats = { 'word_count': 0, 'gunning_fog': 0 }
            if 'page_content' in locals() and page_content and page_content.strip():
                stats['word_count'] = len(page_content.split())
                stats['gunning_fog'] = textstat.gunning_fog(page_content)
            cursor.execute("""
                INSERT INTO content_stats (content_id, user_id, source_type, word_count, gunning_fog)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET
                    word_count = excluded.word_count,
                    gunning_fog = excluded.gunning_fog,
                    last_calculated = CURRENT_TIMESTAMP
            """, (page_id, user_id, 'pages', stats['word_count'], stats['gunning_fog']))
            logger.info(f"[_index_page][{page_id}] Statistiche salvate/aggiornate nella cache.")
        except Exception as e_stats:
            logger.error(f"[_index_page][{page_id}] Errore durante il calcolo/salvataggio delle statistiche: {e_stats}")

    try:
        cursor.execute("UPDATE pages SET processing_status = ? WHERE page_id = ?", (final_status, page_id))
    except sqlite3.Error as db_err:
        logger.error(f"[_index_page][{page_id}] Errore DB: {db_err}")
        return 'failed_db_update'
    return final_status

def _delete_page_permanently(page_id: str, conn: sqlite3.Connection, user_id: Optional[str] = None):
    """
    Elimina completamente una pagina: record DB e chunk da ChromaDB.
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_delete_page][{page_id}] Avvio eliminazione permanente (UserID: {user_id}).")
    cursor = conn.cursor()

    try:
        # 1. Elimina da ChromaDB
        try:
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            base_page_collection_name = "page_content"
            collection_name = f"{base_page_collection_name}_{user_id}" if app_mode == 'saas' else base_page_collection_name
            # Usiamo get_collection. Se non esiste, solleverà un'eccezione che gestiremo.
            page_collection = chroma_client.get_collection(name=collection_name)
            
            chunks_to_delete = page_collection.get(where={"page_id": page_id})
            chunk_ids = chunks_to_delete.get('ids', [])
            if chunk_ids:
                page_collection.delete(ids=chunk_ids)
                logger.info(f"[_delete_page][{page_id}] Eliminati {len(chunk_ids)} chunk da ChromaDB.")
        except Exception as e:
            # Se la collezione non esiste o c'è un altro errore, lo registriamo ma procediamo.
            logger.warning(f"[_delete_page][{page_id}] Errore/avviso durante eliminazione da ChromaDB (procedo comunque): {e}")

        # 2. Elimina dal database SQLite
        cursor.execute("DELETE FROM pages WHERE page_id = ? AND user_id = ?", (page_id, user_id))
        # 3. Elimina dalle statistiche
        cursor.execute("DELETE FROM content_stats WHERE content_id = ? AND user_id = ?", (page_id, user_id))
        logger.info(f"[_delete_page][{page_id}] Record eliminato anche da content_stats.")
        if cursor.rowcount == 0:
            logger.warning(f"[_delete_page][{page_id}] Nessuna riga eliminata dal DB (potrebbe essere già stata cancellata).")
        
        return True

    except Exception as e:
        logger.error(f"[_delete_page][{page_id}] Errore imprevisto durante l'eliminazione: {e}", exc_info=True)
        return False

def _delete_article_permanently(article_id: str, conn: sqlite3.Connection, user_id: Optional[str] = None):
    """
    Elimina completamente un articolo: record DB, chunk da ChromaDB e statistiche.
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_delete_article][{article_id}] Avvio eliminazione permanente (UserID: {user_id}).")
    cursor = conn.cursor()

    try:
        # 1. Elimina da ChromaDB
        try:
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            base_article_collection_name = current_app.config.get('ARTICLE_COLLECTION_NAME', 'article_content')
            collection_name = f"{base_article_collection_name}_{user_id}" if app_mode == 'saas' else base_article_collection_name
            
            article_collection = chroma_client.get_collection(name=collection_name)
            
            chunks_to_delete = article_collection.get(where={"article_id": article_id})
            chunk_ids = chunks_to_delete.get('ids', [])
            if chunk_ids:
                article_collection.delete(ids=chunk_ids)
                logger.info(f"[_delete_article][{article_id}] Eliminati {len(chunk_ids)} chunk da ChromaDB.")
        except Exception as e:
            logger.warning(f"[_delete_article][{article_id}] Errore/avviso durante eliminazione da ChromaDB (procedo comunque): {e}")

        # 2. Elimina dal database SQLite
        cursor.execute("DELETE FROM articles WHERE article_id = ? AND user_id = ?", (article_id, user_id))
        
        # 3. Elimina dalle statistiche
        cursor.execute("DELETE FROM content_stats WHERE content_id = ? AND user_id = ? AND source_type = 'articles'", (article_id, user_id))
        logger.info(f"[_delete_article][{article_id}] Record eliminato anche da content_stats.")
        
        if cursor.rowcount == 0:
            logger.warning(f"[_delete_article][{article_id}] Nessuna riga eliminata dal DB (potrebbe essere già stata cancellata).")
        
        return True

    except Exception as e:
        logger.error(f"[_delete_article][{article_id}] Errore imprevisto durante l'eliminazione: {e}", exc_info=True)
        return False

def _calculate_content_hash(content_text: str) -> str:
    return hashlib.sha256(content_text.encode('utf-8')).hexdigest()

def _background_wp_sync_core(app_context, user_id: str, settings: dict, core_config: dict):
    global wp_sync_status, wp_sync_lock
    
    with app_context:
        db_path = current_app.config.get('DATABASE_FILE')
        conn = None

        with wp_sync_lock:
            wp_sync_status.update({
                'is_processing': True, 'message': 'Connessione a WordPress...', 'error': None,
                'total_items': 0, 'processed_items': 0, 'new_items': 0,
                'updated_items': 0, 'skipped_items': 0, 'deleted_items': 0
            })
            
        try:
            wp_client = WordPressClient(
                site_url=settings['wordpress_url'],
                username=settings['wordpress_username'],
                app_password=settings['wordpress_api_key']
            )
            
            with wp_sync_lock: wp_sync_status['message'] = 'Recupero articoli e pagine dal sito...'
            posts_from_wp = wp_client.get_all_posts()
            pages_from_wp = wp_client.get_all_pages()
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # --- FASE 2: CONFRONTO E PULIZIA ---
            # Pagine (normalizziamo gli URL dal DB e da WP)
            cursor.execute("SELECT page_id, page_url FROM pages WHERE user_id = ?", (user_id,))
            pages_in_db = {}
            for row in cursor.fetchall():
                raw_url = row['page_url']
                normalized = normalize_url(raw_url) if raw_url else raw_url
                pages_in_db[normalized] = {'page_id': row['page_id']}
            urls_from_wp_pages = { normalize_url(page.get('link')) for page in pages_from_wp if page.get('link') }
            pages_to_delete_urls = set(pages_in_db.keys()) - urls_from_wp_pages
            
            deleted_items_count = 0
            if pages_to_delete_urls:
                logger.info(f"Trovate {len(pages_to_delete_urls)} pagine da eliminare.")
                for url in pages_to_delete_urls:
                    page_data_to_delete = pages_in_db.get(url)
                    if not page_data_to_delete:
                        continue
                    page_id_to_delete = page_data_to_delete['page_id']
                    with wp_sync_lock: wp_sync_status['message'] = f'Eliminazione pagina obsoleta: {url[:50]}...'
                    if _delete_page_permanently(page_id_to_delete, conn, user_id):
                        deleted_items_count += 1
                conn.commit()
                with wp_sync_lock: wp_sync_status['deleted_items'] = deleted_items_count
                
            # Articoli (normalizziamo gli URL dal DB e da WP)
            cursor.execute("SELECT article_id, article_url FROM articles WHERE user_id = ?", (user_id,))
            articles_in_db = {}
            for row in cursor.fetchall():
                raw_url = row['article_url']
                normalized = normalize_url(raw_url) if raw_url else raw_url
                articles_in_db[normalized] = row['article_id']
            urls_from_wp_posts = { normalize_url(post.get('link')) for post in posts_from_wp if post.get('link') }
            articles_to_delete_urls = set(articles_in_db.keys()) - urls_from_wp_posts

            if articles_to_delete_urls:
                logger.info(f"Trovati {len(articles_to_delete_urls)} articoli da eliminare. ESECUZIONE ELIMINAZIONE.")
                for url in articles_to_delete_urls:
                    article_id_to_delete = articles_in_db.get(url)
                    if not article_id_to_delete:
                        continue
                    with wp_sync_lock: wp_sync_status['message'] = f'Eliminazione articolo obsoleto: {url[:50]}...'
                    if _delete_article_permanently(article_id_to_delete, conn, user_id):
                        deleted_items_count += 1
                conn.commit()
                with wp_sync_lock: wp_sync_status['deleted_items'] = deleted_items_count

            # --- FASE 3: AGGIUNTA E AGGIORNAMENTO (invariata) ---
            all_items = [('post', item) for item in posts_from_wp] + [('page', item) for item in pages_from_wp]
            total_items_to_process = len(all_items)
            with wp_sync_lock: wp_sync_status['total_items'] = total_items_to_process

            new_items_count, updated_items_count, skipped_items_count = 0, 0, 0

            for idx, (item_type, item_data) in enumerate(all_items):
                processed_count = idx + 1
                title = html.unescape(item_data.get('title', {}).get('rendered', 'Senza Titolo'))
                item_url_raw = item_data.get('link')
                item_url = normalize_url(item_url_raw) if item_url_raw else item_url_raw
                content_html = item_data.get('content', {}).get('rendered', '')
                soup = BeautifulSoup(content_html, 'html.parser')
                content_text = soup.get_text(separator='\n', strip=True)                
                if len(content_text.split()) < 10:
                    logger.info(f"Saltato {item_type} '{title}' (URL: {item_url}) - contenuto troppo breve o vuoto ({len(content_text.split())} parole).")
                    with wp_sync_lock: wp_sync_status['skipped_items'] += 1
                    continue

                current_hash = hashlib.sha256(content_text.encode('utf-8')).hexdigest()
                
                date_str = None
                if item_type == 'post':
                    date_str = item_data.get('date_gmt')
                else:
                    date_str = item_data.get('modified_gmt')
                
                published_at_iso = f"{date_str}Z" if date_str else None

                with wp_sync_lock:
                    wp_sync_status['message'] = f"Controllo {item_type} ({processed_count}/{total_items_to_process}): {title[:40]}..."
                    wp_sync_status['processed_items'] = processed_count

                table_name = 'articles' if item_type == 'post' else 'pages'
                id_col = 'article_id' if item_type == 'post' else 'page_id'
                url_col = 'article_url' if item_type == 'post' else 'page_url'

                cursor.execute(f"SELECT {id_col}, content_hash FROM {table_name} WHERE {url_col} = ? AND user_id = ?", (item_url, user_id))
                existing = cursor.fetchone()

                if existing:
                    if existing['content_hash'] != current_hash:
                        logger.info(f"{item_type.capitalize()} '{title}' modificato. Aggiornamento.")
                        item_id = existing[id_col]
                        cursor.execute(f"UPDATE {table_name} SET title = ?, published_at = ?, content = ?, content_hash = ?, processing_status = 'pending' WHERE {id_col} = ?",
                                    (title, published_at_iso, content_text, current_hash, item_id))
                        if item_type == 'post':
                            _index_article(item_id, conn, user_id, core_config)
                        else:
                            _index_page(item_id, conn, user_id, core_config)
                        updated_items_count += 1
                    else:
                        skipped_items_count += 1
                else:
                    logger.info(f"Nuovo {item_type} '{title}'. Creazione.")
                    item_id = str(uuid.uuid4())
                    try:
                        if item_type == 'post':
                            cursor.execute("INSERT OR IGNORE INTO articles (article_id, guid, feed_url, article_url, title, published_at, content, content_hash, user_id, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                                        (item_id, item_data.get('guid', {}).get('rendered', item_url), settings['wordpress_url'], item_url, title, published_at_iso, content_text, current_hash, user_id))
                            # Se l'insert è stato ignorato (rowcount == 0), recupera l'id esistente
                            if cursor.rowcount == 0:
                                cursor.execute("SELECT article_id FROM articles WHERE article_url = ? AND user_id = ?", (item_url, user_id))
                                existing_row = cursor.fetchone()
                                if existing_row:
                                    logger.info(f"Articolo '{title}' già presente dopo INSERT OR IGNORE.")
                                    skipped_items_count += 1
                                else:
                                    new_items_count += 1
                            else:
                                _index_article(item_id, conn, user_id, core_config)
                                new_items_count += 1
                        else:
                            cursor.execute("INSERT OR IGNORE INTO pages (page_id, page_url, title, published_at, content, content_hash, user_id, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                                        (item_id, item_url, title, published_at_iso, content_text, current_hash, user_id))
                            if cursor.rowcount == 0:
                                cursor.execute("SELECT page_id FROM pages WHERE page_url = ? AND user_id = ?", (item_url, user_id))
                                existing_row = cursor.fetchone()
                                if existing_row:
                                    logger.info(f"Pagina '{title}' già presente dopo INSERT OR IGNORE.")
                                    skipped_items_count += 1
                                else:
                                    new_items_count += 1
                            else:
                                _index_page(item_id, conn, user_id, core_config)
                                new_items_count += 1
                    except sqlite3.IntegrityError as ie:
                        logger.warning(f"IntegrityError inserimento {item_type} '{title}': {ie}")
                        skipped_items_count += 1
                    except Exception as e_insert:
                        logger.error(f"Errore inserimento nuovo {item_type} '{title}': {e_insert}", exc_info=True)
                        skipped_items_count += 1
                conn.commit()

            with wp_sync_lock:
                wp_sync_status['new_items'] = new_items_count
                wp_sync_status['updated_items'] = updated_items_count
                wp_sync_status['skipped_items'] = skipped_items_count
                final_message = f"Sincronizzazione completata! ({new_items_count} nuovi, {updated_items_count} aggiornati, {deleted_items_count} eliminati)."
                wp_sync_status.update({'is_processing': False, 'message': final_message})
        
        except Exception as e:
            error_message = f"Errore: {e}"
            if '401' in str(e) or '403' in str(e): error_message = 'Errore di autenticazione. Controlla le credenziali.'
            logger.error(f"Errore durante la sincronizzazione WordPress per utente {user_id}: {e}", exc_info=True)
            with wp_sync_lock:
                wp_sync_status.update({'is_processing': False, 'error': error_message, 'message': "Sincronizzazione fallita."})
            if conn: conn.rollback()
        finally:
            if conn:
                conn.close()

@connectors_bp.route('/wordpress/sync', methods=['POST'])
@login_required
def sync_wordpress():
    global wp_sync_status, wp_sync_lock
    user_id = current_user.id
    
    with wp_sync_lock:
        if wp_sync_status['is_processing']:
            return jsonify({'success': False, 'message': 'Una sincronizzazione WordPress è già in corso.'}), 409

        # --- MODIFICA CHIAVE 1: IMPOSTIAMO SUBITO LO STATO DI "LAVORO IN CORSO" ---
        # Questo assicura che qualsiasi richiesta di stato immediata veda che siamo occupati.
        wp_sync_status.update({
            'is_processing': True,
            'message': 'Avvio sincronizzazione...',
            'error': None,
            'total_items': 0,
            'processed_items': 0,
            'new_items': 0,
            'updated_items': 0,
            'skipped_items': 0,
            'deleted_items': 0
        })

    db_path = current_app.config.get('DATABASE_FILE')
    settings_row = None
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT wordpress_url, wordpress_username, wordpress_api_key FROM user_settings WHERE user_id = ?", (user_id,))
        settings_row = cursor.fetchone()
    finally:
        if conn: conn.close()
    
    settings_dict = dict(settings_row) if settings_row else None
    
    if not settings_dict or not all(settings_dict.values()):
        # Se fallisce, dobbiamo resettare lo stato a "non in elaborazione"
        with wp_sync_lock:
            wp_sync_status['is_processing'] = False
        return jsonify({'success': False, 'message': 'Configurazione WordPress incompleta. Controlla URL, nome utente e Application Password nelle Impostazioni.'}), 400

    core_config_dict = build_full_config_for_background_process(user_id)
    app_context = current_app.app_context()
    background_thread = threading.Thread(
        target=_background_wp_sync_core,
        args=(app_context, user_id, settings_dict, core_config_dict)
    )
    background_thread.daemon = True
    background_thread.start()
    
    logger.info(f"Avviato thread di sincronizzazione WordPress per l'utente: {user_id}")
    # --- MODIFICA CHIAVE 2: La risposta ora è coerente con lo stato appena impostato ---
    return jsonify({'success': True, 'message': 'Sincronizzazione avviata in background.'}), 202

# --- NUOVO ENDPOINT PER IL POLLING ---
# Questo è l'endpoint che il frontend chiamerà per avere aggiornamenti.
@connectors_bp.route('/wordpress/progress', methods=['GET'])
@login_required
def get_wordpress_sync_progress():
    global wp_sync_status, wp_sync_lock
    with wp_sync_lock:
        # Creiamo una copia per evitare problemi di concorrenza
        status_copy = copy.deepcopy(wp_sync_status)
        # Restituiamo solo i dati essenziali per la UI
        response_data = {
            'is_processing': status_copy.get('is_processing', False),
            'message': status_copy.get('message', ''),
            'error': status_copy.get('error', None),
            'total_items': status_copy.get('total_items', 0),
            'processed_items': status_copy.get('processed_items', 0)
        }
        return jsonify(response_data)
    
@connectors_bp.route('/pages/all', methods=['DELETE'])
@login_required
def delete_all_user_pages():
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

        cursor.execute("SELECT page_id FROM pages WHERE user_id = ?", (user_id,))
        page_ids_to_delete = [row[0] for row in cursor.fetchall()]
        
        if not page_ids_to_delete:
            return jsonify({'success': True, 'message': 'Nessuna pagina da eliminare.'})

        for page_id in page_ids_to_delete:
            if _delete_page_permanently(page_id, conn, user_id):
                deleted_count += 1
        
        conn.commit()
        
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
    app_mode = current_app.config.get('APP_MODE', 'single')
    if app_mode != 'saas':
        return jsonify({'success': False, 'message': 'Operazione permessa solo in modalità SAAS.'}), 403
    
    user_id = current_user.id
    logger.info(f"Richiesta eliminazione singola pagina ID: {page_id} per utente: {user_id}")

    conn = None
    try:
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        
        success = _delete_page_permanently(page_id, conn, user_id)
        
        if success:
            conn.commit()
            message = "Pagina eliminata con successo."
            logger.info(message)
            return jsonify({'success': True, 'message': message})
        else:
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

        sql_query = "SELECT page_id, title, page_url, content FROM pages WHERE processing_status = 'completed' AND content IS NOT NULL"
        params = []
        if app_mode == 'saas':
            sql_query += " AND user_id = ?"
            params.append(current_user_id)
        
        cursor.execute(sql_query, tuple(params))

        for row in cursor.fetchall():
            content = row['content']
            if content:
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
