import logging
import feedparser
from urllib.error import HTTPError
import requests
from bs4 import BeautifulSoup
import sqlite3
import uuid
from typing import Optional, Dict
from flask_login import login_required, current_user
import os
import datetime
from urllib.parse import urlparse, urljoin
from flask import Blueprint, request, jsonify, current_app, Response
from google.api_core import exceptions as google_exceptions
import threading
import textstat
import copy
import logging 
import io
from app.services.chunking.agentic_chunker import chunk_text_agentically
from app.services.embedding.embedding_service import generate_embeddings
from app.utils import build_full_config_for_background_process, normalize_url

logger = logging.getLogger(__name__)

try:
    from app.services.embedding.gemini_embedding import split_text_into_chunks, get_gemini_embeddings, TASK_TYPE_DOCUMENT
except ImportError:
    # ... (gestione errore import) ...
    logger.error("!!! Impossibile importare funzioni di embedding/chunking (rss.py) !!!")
    split_text_into_chunks = None
    get_gemini_embeddings = None
    TASK_TYPE_DOCUMENT = "retrieval_document"

rss_bp = Blueprint('rss', __name__)

# --- STATO GLOBALE e LOCK per Processo RSS ---
rss_processing_status = {
    'is_processing': False,
    'current_page': 0,          # Pagina feed attualmente in analisi
    'total_articles_processed': 0, # Articoli nuovi/riprovati in questo run
    'message': '',              # Messaggio di stato leggibile
    'error': None               # Stringa di errore se il thread fallisce
}
rss_status_lock = threading.Lock() # Lock per aggiornamenti sicuri

# --- Funzioni Helper (is_valid_url, get_full_article_content, parse_feed_date, _index_article) ---
# ... (queste rimangono invariate) ...
def is_valid_url(url):
    """Verifica base se una stringa sembra un URL HTTP/HTTPS."""
    try:
        result = urlparse(url)
        return all([result.scheme in ['http', 'https'], result.netloc])
    except ValueError:
        return False

def get_full_article_content(article_url):
    """
    Tenta di scaricare e estrarre il contenuto principale da un URL.
    ATTENZIONE: Questo è semplice web scraping, può fallire facilmente.
    Restituisce il testo o None.
    """
    if not article_url or not is_valid_url(article_url):
        return None
    try:
        headers = {'User-Agent': 'MagazzinoDelCreatoreBot/1.0'} # Buona pratica
        response = requests.get(article_url, headers=headers, timeout=15) # Timeout
        response.raise_for_status() # Solleva eccezione per errori HTTP

        # Usa BeautifulSoup per trovare il contenuto principale (euristica)
        soup = BeautifulSoup(response.content, 'html.parser')

        # Tenta diverse strategie comuni per trovare il contenuto principale
        article_body = soup.find('article') or \
                       soup.find('main') or \
                       soup.find('div', class_=lambda x: x and 'content' in x) or \
                       soup.find('div', id='content') or \
                       soup.find('div', class_='post-content') # Aggiungi altri selettori comuni

        if article_body:
             # Estrai testo, rimuovendo script/style e normalizzando spazi
            for tag in article_body(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                tag.decompose() # Rimuovi tag non di contenuto
            text = article_body.get_text(separator='\n', strip=True)
            # Ulteriore pulizia base
            text = '\n'.join(line.strip() for line in text.splitlines() if line.strip())
            logger.info(f"Estratto contenuto da URL: {article_url} (Lunghezza: {len(text)})")
            return text
        else:
            logger.warning(f"Contenuto principale non trovato in {article_url} con selettori base.")
            # Come fallback, potremmo prendere tutto il body? Rischioso.
            # body_text = soup.body.get_text(separator='\n', strip=True) if soup.body else None
            # return '\n'.join(line.strip() for line in (body_text or "").splitlines() if line.strip())
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Errore scaricando {article_url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Errore parsing HTML da {article_url}: {e}", exc_info=True)
        return None

def parse_feed_date(feed_date_struct):
    """Converte la struttura temporale di feedparser in stringa ISO 8601."""
    if not feed_date_struct:
        return None
    try:
        # feedparser restituisce una struct_time
        dt = datetime.datetime(*feed_date_struct[:6])
        return dt.isoformat()
    except (TypeError, ValueError):
        return None # Restituisce None se la data non è valida

def _index_article(article_id: str, conn: sqlite3.Connection, user_id: str, core_config: dict) -> str:
    """
    Esegue l'indicizzazione di un articolo. Richiede sempre un user_id.
    """
    logger.info(f"[_index_article][{article_id}] Avvio indicizzazione per UserID: {user_id}")

    final_status = 'failed_indexing_init'
    cursor = conn.cursor()

    llm_api_key = core_config.get('GOOGLE_API_KEY')
    embedding_model = core_config.get('GEMINI_EMBEDDING_MODEL')
    chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
    chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
    base_article_collection_name = core_config.get('ARTICLE_COLLECTION_NAME', 'article_content')
    
    chroma_client = core_config.get('CHROMA_CLIENT')
    if not chroma_client: 
        return 'failed_config_client_missing'
    if not user_id:
        return 'failed_user_id_missing'
        
    user_article_collection_name = f"{base_article_collection_name}_{user_id}"
    try:
        article_collection = chroma_client.get_or_create_collection(name=user_article_collection_name)
    except Exception as e_coll: 
        logger.error(f"[_index_article][{article_id}] Errore get/create collezione '{user_article_collection_name}': {e_coll}")
        return 'failed_chroma_collection'
    
    logger.info(f"[_index_article][{article_id}] Collezione Chroma '{user_article_collection_name}' pronta.")

    try:
        cursor.execute("SELECT content, title, article_url FROM articles WHERE article_id = ?", (article_id,))
        article_data = cursor.fetchone()
        if not article_data: 
            logger.error(f"[_index_article][{article_id}] Record non trovato nel DB.")
            return 'failed_article_not_found'
        
        article_content, title, article_url = article_data[0], article_data[1], article_data[2]
        
        if not article_content or not article_content.strip():
             final_status = 'completed'
        else:
            use_agentic_chunking = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'
            chunks = []
            if use_agentic_chunking:
                try:
                    chunks = chunk_text_agentically(article_content, llm_provider=core_config.get('llm_provider', 'google'), settings=core_config)
                except google_exceptions.ResourceExhausted as e:
                    chunks = []
                if not chunks:
                    chunks = split_text_into_chunks(article_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            else:
                chunks = split_text_into_chunks(article_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            
            if not chunks:
                 final_status = 'completed'
            else:
                embeddings = generate_embeddings(chunks, user_settings=core_config, task_type=TASK_TYPE_DOCUMENT)
                if not embeddings or len(embeddings) != len(chunks):
                    final_status = 'failed_embedding'
                else:
                    ids = [f"{article_id}_chunk_{i}" for i in range(len(chunks))]
                    metadatas_chroma = [{
                        "article_id": article_id, "article_title": title, "article_url": article_url,
                        "chunk_index": i, "source_type": "article",
                        "user_id": user_id
                    } for i in range(len(chunks))]
                    article_collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas_chroma, documents=chunks)
                    final_status = 'completed'
    except Exception as e:
        logger.error(f"[_index_article][{article_id}] Errore imprevisto durante indicizzazione: {e}", exc_info=True)
        # Ripristiniamo la logica per dare un codice di errore più specifico
        if 'split_text_into_chunks' in str(e): final_status = 'failed_chunking'
        elif 'upsert' in str(e): final_status = 'failed_chroma_write'
        else: final_status = 'failed_processing_generic'
    
    if final_status == 'completed':
        try:
            stats = { 'word_count': 0, 'gunning_fog': 0 }
            if 'article_content' in locals() and article_content and article_content.strip():
                stats['word_count'] = len(article_content.split())
                stats['gunning_fog'] = textstat.gunning_fog(article_content)
            cursor.execute("""
                INSERT INTO content_stats (content_id, user_id, source_type, word_count, gunning_fog)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET
                    word_count = excluded.word_count,
                    gunning_fog = excluded.gunning_fog,
                    last_calculated = CURRENT_TIMESTAMP
            """, (article_id, user_id, 'articles', stats['word_count'], stats['gunning_fog']))
        except Exception as e_stats:
            logger.error(f"[_index_article][{article_id}] Errore durante il calcolo/salvataggio delle statistiche: {e_stats}")

    try:
        cursor.execute("UPDATE articles SET processing_status = ? WHERE article_id = ?", (final_status, article_id))
    except sqlite3.Error as db_update_err:
         final_status = 'failed_db_status_update'

    return final_status

def _process_rss_feed_core(
    initial_feed_url: str, 
    user_id: str, 
    core_config: dict, 
    status_dict: Optional[dict] = None,
    status_lock: Optional[threading.Lock] = None
) -> bool:
    logger.info(f"[CORE RSS Process] Avvio per feed={initial_feed_url}, user_id={user_id}")
    overall_success = False
    conn_sqlite = None

    pages_processed, total_entries, skipped_count, saved_ok_count, failed_count = 0, 0, 0, 0, 0

    try:
        db_path = current_app.config.get('DATABASE_FILE')
        conn_sqlite = sqlite3.connect(db_path, timeout=10.0)
        conn_sqlite.row_factory = sqlite3.Row
        cursor_sqlite = conn_sqlite.cursor()

        articles_map = {}
        cursor_sqlite.execute("SELECT article_id, article_url, processing_status FROM articles WHERE user_id = ?", (user_id,))
        for r in cursor_sqlite.fetchall():
            norm_db_url = normalize_url(r['article_url']) if r['article_url'] else r['article_url']
            articles_map[norm_db_url] = {'article_id': r['article_id'], 'processing_status': r['processing_status']}

        parsed_initial_url = urlparse(initial_feed_url)
        base_feed_url = urljoin(initial_feed_url, parsed_initial_url.path)
        page_number = 1

        while True:
            url_to_fetch = f"{base_feed_url}{'&' if '?' in base_feed_url else '?'}paged={page_number}" if page_number > 1 else base_feed_url

            if status_dict and status_lock:
                with status_lock:
                    status_dict['message'] = f"Analisi pagina #{page_number} del feed..."

            parsed_feed = feedparser.parse(url_to_fetch, request_headers={'User-Agent': 'MagazzinoDelCreatoreBot/1.0'}, agent='MagazzinoDelCreatoreBot/1.0')

            if parsed_feed.bozo or not parsed_feed.entries:
                break

            pages_processed += 1
            num_entries_page = len(parsed_feed.entries)
            total_entries += num_entries_page

            for entry_index, entry in enumerate(parsed_feed.entries):
                if status_dict and status_lock:
                    with status_lock:
                        status_dict['message'] = f"Articolo {entry_index + 1}/{num_entries_page} (Pag. {page_number}): {entry.get('title', 'Senza Titolo')[:50]}..."

                article_url = entry.get('link')
                title = entry.get('title', 'Senza Titolo')
                article_id_to_process = None
                needs_processing = False

                if not article_url:
                    skipped_count += 1
                    continue

                norm_article_url = normalize_url(article_url)
                existing = articles_map.get(norm_article_url)

                if existing:
                    if existing['processing_status'] == 'pending' or (existing['processing_status'] and str(existing['processing_status']).startswith('failed_')):
                        article_id_to_process = existing['article_id']
                        needs_processing = True
                    else:
                        skipped_count += 1
                        continue
                else:
                    content = None
                    if 'content' in entry and entry.get('content'):
                        soup_content = BeautifulSoup(entry.content[0].value, 'html.parser')
                        content = '\n'.join(line.strip() for line in soup_content.get_text(separator='\n', strip=True).splitlines() if line.strip())
                    elif 'summary' in entry and entry.get('summary'):
                        soup_summary = BeautifulSoup(entry.summary, 'html.parser')
                        content = '\n'.join(line.strip() for line in soup_summary.get_text(separator='\n', strip=True).splitlines() if line.strip())
                    
                    full_content = get_full_article_content(article_url)
                    content = full_content if full_content else content

                    if not content:
                        skipped_count += 1
                        continue

                    article_id_to_process = str(uuid.uuid4())
                    needs_processing = True
                    guid = entry.get('id') or entry.get('guid') or article_url
                    published_at_iso = parse_feed_date(entry.get('published_parsed') or entry.get('updated_parsed'))

                    cursor_sqlite.execute("INSERT INTO articles (article_id, guid, feed_url, article_url, title, published_at, content, user_id, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (article_id_to_process, guid, initial_feed_url, article_url, title, published_at_iso, content, user_id, 'pending'))
                    articles_map[norm_article_url] = {'article_id': article_id_to_process, 'processing_status': 'pending'}

                if needs_processing and article_id_to_process:
                    indexing_status = _index_article(article_id_to_process, conn_sqlite, user_id, core_config)
                    if indexing_status == 'completed':
                        saved_ok_count += 1
                    else:
                        failed_count += 1
                    articles_map[norm_article_url]['processing_status'] = indexing_status

            page_number += 1

        conn_sqlite.commit()
        overall_success = True

    except Exception as e_core_generic:
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    finally:
        if conn_sqlite: conn_sqlite.close()

    return overall_success


# --- Funzione Background per RSS ---
def _background_rss_processing(app_context, initial_feed_url: str, user_id: Optional[str], initial_status: dict):
    global rss_processing_status, rss_status_lock
    thread_final_message = "Elaborazione background RSS terminata con stato sconosciuto."
    job_success = False

    with rss_status_lock:
        rss_processing_status.update(initial_status)
        rss_processing_status['is_processing'] = True
        rss_processing_status['message'] = 'Avvio elaborazione feed...'
        rss_processing_status['error'] = None
    logger.info(f"BACKGROUND THREAD RSS: Avvio per URL={initial_feed_url}, user_id={user_id}")

    with app_context: # Esegui nel contesto app
        try:
            core_config_dict = build_full_config_for_background_process(user_id)
            # Verifica che i valori essenziali non siano None (opzionale ma buona pratica)
            required_keys_rss = ['DATABASE_FILE', 'ARTICLES_FOLDER_PATH', 'GOOGLE_API_KEY', 'CHROMA_CLIENT']
            missing_keys_rss = [k for k in required_keys_rss if not core_config_dict.get(k)]
            if missing_keys_rss:
                 raise RuntimeError(f"Valori mancanti nella config per il thread RSS: {', '.join(missing_keys_rss)}")
            logger.info("BACKGROUND THREAD RSS: Dizionario 'core_config_dict' preparato.")
            # === FINE COSTRUZIONE core_config ===

            # --- Chiama la Funzione Core PASSANDO core_config_dict ---
            job_success = _process_rss_feed_core(
                initial_feed_url, 
                user_id, 
                core_config_dict,
                status_dict=rss_processing_status, # Aggiungiamo questo
                status_lock=rss_status_lock        # Aggiungiamo questo
            )

            if job_success:
                 thread_final_message = f"Processo feed {initial_feed_url} completato."
            else:
                 thread_final_message = f"Si sono verificati errori durante il processo del feed {initial_feed_url}. Controllare i log del server."

        except Exception as e_thread:
            # È importante loggare l'eccezione completa qui per capire cosa è successo
            logger.exception(f"BACKGROUND THREAD RSS: ERRORE CRITICO - {e_thread}") # Modificato per loggare e_thread
            thread_final_message = f"Errore critico elaborazione feed: {e_thread}"
            job_success = False
        finally:
            logger.info(f"BACKGROUND THREAD RSS: Esecuzione finally.")
            with rss_status_lock:
                rss_processing_status['is_processing'] = False
                rss_processing_status['message'] = thread_final_message
                rss_processing_status['error'] = None if job_success else thread_final_message
                rss_processing_status['current_page'] = 0
            logger.info(f"BACKGROUND THREAD RSS: Terminato. Successo Core Job: {job_success}. Messaggio UI: {thread_final_message}")

@rss_bp.route('/process', methods=['POST'])
@login_required
def process_rss_feed():
    global rss_processing_status, rss_status_lock
    current_user_id = current_user.id

    with rss_status_lock:
        if rss_processing_status.get('is_processing', False):
            return jsonify({'success': False, 'error_code': 'ALREADY_PROCESSING', 'message': 'Un processo RSS è già attivo.'}), 409

        rss_processing_status.update({
            'is_processing': True,
            'current_page': 0,
            'total_articles_processed': 0,
            'message': 'Avvio elaborazione feed in background...',
            'error': None
        })

    initial_feed_url = request.json.get('rss_url')
    if not initial_feed_url or not is_valid_url(initial_feed_url):
        with rss_status_lock: rss_processing_status['is_processing'] = False
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': "URL non valido."}), 400

    try:
        app_context = current_app.app_context()
        background_thread = threading.Thread(
            target=_background_rss_processing,
            args=(app_context, initial_feed_url, current_user_id, copy.deepcopy(rss_processing_status))
        )
        background_thread.daemon = True
        background_thread.start()
        return jsonify({'success': True, 'message': 'Elaborazione avviata in background.'}), 202

    except Exception as e_start:
        with rss_status_lock:
            rss_processing_status['is_processing'] = False
        return jsonify({'success': False, 'error_code': 'THREAD_START_FAILED', 'message': f'Errore avvio processo: {e_start}'}), 500
    
# --- Polling Stato RSS ---
@rss_bp.route('/progress', methods=['GET'])
@login_required # O rimuovi se non necessario
def get_rss_progress():
    """Restituisce lo stato attuale del processo RSS in background."""
    global rss_processing_status, rss_status_lock
    with rss_status_lock: # Leggi stato in modo sicuro
        current_status = copy.deepcopy(rss_processing_status)
    return jsonify(current_status)

@rss_bp.route('/download_all_articles', methods=['GET'])
@login_required
def download_all_articles():
    db_path = current_app.config.get('DATABASE_FILE')
    current_user_id = current_user.id

    all_articles_content = io.StringIO()
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT article_id, title, article_url, content FROM articles WHERE processing_status = 'completed' AND user_id = ? ORDER BY published_at DESC", (current_user_id,))

        for row in cursor.fetchall():
            content = row['content']
            if content:
                all_articles_content.write(f"--- ARTICOLO START ---\n")
                all_articles_content.write(f"ID: {row['article_id']}\n")
                all_articles_content.write(f"Titolo: {row['title']}\n")
                all_articles_content.write(f"URL: {row['article_url']}\n")
                all_articles_content.write(f"--- Contenuto ---\n{content}\n")
                all_articles_content.write(f"--- ARTICOLO END ---\n\n\n")
        
        conn.close()

    except Exception as e:
         if conn: conn.close()
         return jsonify({'success': False, 'error': 'Errore durante la preparazione del download.'}), 500

    output_filename = f"articles_{current_user_id}.txt"
    file_content = all_articles_content.getvalue()
    all_articles_content.close()

    return Response(
        file_content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment;filename={output_filename}"}
    )

@rss_bp.route('/all', methods=['DELETE'])
@login_required
def delete_all_user_articles():
    current_user_id = current_user.id
    logger.info(f"Avvio eliminazione di massa articoli per utente: {current_user_id}")

    db_path = current_app.config.get('DATABASE_FILE')
    base_article_collection_name = current_app.config.get('ARTICLE_COLLECTION_NAME', 'article_content')
    chroma_client = current_app.config.get('CHROMA_CLIENT')

    user_article_collection_name = f"{base_article_collection_name}_{current_user_id}"
    conn_sqlite = None
    try:
        conn_sqlite = sqlite3.connect(db_path)
        cursor_sqlite = conn_sqlite.cursor()

        cursor_sqlite.execute("DELETE FROM articles WHERE user_id = ?", (current_user_id,))
        sqlite_rows_affected = cursor_sqlite.rowcount
        
        cursor_sqlite.execute("DELETE FROM content_stats WHERE user_id = ? AND source_type = 'articles'", (current_user_id,))
        conn_sqlite.commit()
        
    except sqlite3.Error as e_sql:
        if conn_sqlite: conn_sqlite.rollback()
        return jsonify({'success': False, 'error_code': 'DB_DELETE_FAILED', 'message': f'Errore DB durante eliminazione: {e_sql}'}), 500
    finally:
         if conn_sqlite: conn_sqlite.close()

    try:
        chroma_client.delete_collection(name=user_article_collection_name)
    except Exception as e_chroma:
        logger.warning(f"Errore durante eliminazione collezione ChromaDB '{user_article_collection_name}': {e_chroma}")

    return jsonify({
        'success': True,
        'message': f"Eliminazione articoli utente {current_user_id} completata. Record SQLite affetti: {sqlite_rows_affected}."
    }), 200

@rss_bp.route('/debug_summary', methods=['GET'])
@login_required
def debug_rss_summary():
    db_path = current_app.config.get('DATABASE_FILE')
    current_user_id = current_user.id

    result = {
        'user_id': current_user_id,
        'db': {},
        'sample_articles': [],
        'chroma': {}
    }

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) as c FROM articles WHERE user_id = ?", (current_user_id,))
        result['db']['total_articles'] = cur.fetchone()['c']

        cur.execute("SELECT processing_status, COUNT(*) as c FROM articles WHERE user_id = ? GROUP BY processing_status", (current_user_id,))
        result['db']['by_status'] = {row['processing_status'] or 'NULL': row['c'] for row in cur.fetchall()}

        cur.execute("SELECT article_id, title, article_url, processing_status, added_at FROM articles WHERE user_id = ? ORDER BY added_at DESC LIMIT 10", (current_user_id,))
        result['sample_articles'] = [dict(r) for r in cur.fetchall()]

    except Exception as e:
        result['db']['error'] = str(e)
    finally:
        if conn:
            conn.close()

    try:
        chroma_client = current_app.config.get('CHROMA_CLIENT')
        base_name = current_app.config.get('ARTICLE_COLLECTION_NAME', 'article_content')
        collection_name = f"{base_name}_{current_user_id}"
        result['chroma']['collection_name'] = collection_name

        if not chroma_client:
            result['chroma']['error'] = 'Chroma client non configurato'
        else:
            try:
                coll = chroma_client.get_collection(name=collection_name)
                result['chroma']['document_count'] = coll.count()
            except Exception as e_coll:
                result['chroma']['error'] = str(e_coll)
    except Exception as e:
        result['chroma']['error'] = str(e)

    return jsonify(result)
