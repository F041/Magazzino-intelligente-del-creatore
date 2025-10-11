# --- INIZIO FILE app/api/routes/rss.py ---
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

def _index_article(article_id: str, conn: sqlite3.Connection, user_id: Optional[str], core_config: dict) -> str:
    """
    Esegue l'indicizzazione di un articolo (legge contenuto, chunk, embed, Chroma).
    Gestisce modalità 'single' e 'saas'.
    NON fa commit; si aspetta che il chiamante gestisca la transazione.
    Restituisce lo stato finale ('completed' o 'failed_...').
    """
    # --- MODIFICA 1: Usa core_config invece di current_app ---
    app_mode = core_config.get('APP_MODE', 'single')
    logger.info(f"[_index_article][{article_id}] Avvio indicizzazione (Modalità: {app_mode}, UserID: {user_id if user_id else 'N/A'})")

    final_status = 'failed_indexing_init'
    cursor = conn.cursor()

    # --- MODIFICA 2: Recupera tutte le configurazioni da core_config ---
    llm_api_key = core_config.get('GOOGLE_API_KEY')
    embedding_model = core_config.get('GEMINI_EMBEDDING_MODEL')
    chunk_size = core_config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
    chunk_overlap = core_config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
    base_article_collection_name = core_config.get('ARTICLE_COLLECTION_NAME', 'article_content')

    # Ottieni Collezione ChromaDB
    article_collection = None
    collection_name_for_log = "N/A"
    
    # --- MODIFICA 3: Usa core_config per il client ChromaDB ---
    chroma_client = core_config.get('CHROMA_CLIENT')
    if not chroma_client: 
        logger.error(f"[_index_article][{article_id}] Chroma Client non trovato in core_config!")
        return 'failed_config_client_missing'

    if app_mode == 'single':
        try:
            article_collection = chroma_client.get_or_create_collection(name=base_article_collection_name)
            if article_collection: collection_name_for_log = article_collection.name
        except Exception as e:
             logger.error(f"[_index_article][{article_id}] Modalità SINGLE: Errore get/create collezione: {e}")
             return 'failed_config_collection_missing'
    elif app_mode == 'saas':
        if not user_id: 
            logger.error(f"[_index_article][{article_id}] Modalità SAAS: User ID mancante!")
            return 'failed_user_id_missing'
        
        user_article_collection_name = f"{base_article_collection_name}_{user_id}"
        collection_name_for_log = user_article_collection_name
        try:
            article_collection = chroma_client.get_or_create_collection(name=user_article_collection_name)
        except Exception as e_saas_coll: 
            logger.error(f"[_index_article][{article_id}] Modalità SAAS: Errore get/create collezione '{user_article_collection_name}': {e_saas_coll}")
            return 'failed_chroma_collection_saas'
    else: 
        logger.error(f"[_index_article][{article_id}] Modalità APP non valida: {app_mode}")
        return 'failed_invalid_mode'

    if not article_collection: 
        logger.error(f"[_index_article][{article_id}] Fallimento ottenimento collezione ChromaDB (Nome tentato: {collection_name_for_log}).")
        return 'failed_chroma_collection_generic'
    
    logger.info(f"[_index_article][{article_id}] Collezione Chroma '{collection_name_for_log}' pronta.")

    if not llm_api_key or not embedding_model: 
        logger.error(f"[_index_article][{article_id}] Configurazione Embedding mancante.")
        return 'failed_config_embedding'
    
    # Logica Indicizzazione (invariata, ma ora usa percorsi relativi)
    try:
        cursor.execute("SELECT content, title, article_url FROM articles WHERE article_id = ?", (article_id,))
        article_data = cursor.fetchone()
        if not article_data: 
            logger.error(f"[_index_article][{article_id}] Record non trovato nel DB.")
            return 'failed_article_not_found'
        
        article_content, title, article_url = article_data[0], article_data[1], article_data[2]
        logger.info(f"[_index_article][{article_id}] Trovato articolo '{title}' nel DB.")
        
        if not article_content or not article_content.strip():
             logger.warning(f"[_index_article][{article_id}] File contenuto vuoto. Marco come completato.")
             final_status = 'completed'
        else:
            logger.info(f"[_index_article][{article_id}] Contenuto letto ({len(article_content)} chars).")
            # --- INIZIO NUOVA LOGICA DI CHUNKING CONDIZIONALE ---
            use_agentic_chunking = str(core_config.get('USE_AGENTIC_CHUNKING', 'False')).lower() == 'true'

            chunks = [] # Inizializziamo a lista vuota
            if use_agentic_chunking:
                try:
                    logger.info(f"[_index_article][{article_id}] Tentativo di CHUNKING INTELLIGENTE (Agentic)...")
                    chunks = chunk_text_agentically(article_content, llm_provider=core_config.get('llm_provider', 'google'), settings=core_config)
                except google_exceptions.ResourceExhausted as e:
                    logger.warning(f"[_index_article][{article_id}] Quota API esaurita durante il chunking. Fallback al metodo classico. Errore: {e}")
                    chunks = [] # Resetta per sicurezza

                if not chunks:
                    logger.warning(f"[_index_article][{article_id}] CHUNKING INTELLIGENTE non ha prodotto risultati. Ritorno al metodo classico.")
                    chunks = split_text_into_chunks(article_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            else:
                logger.info(f"[_index_article][{article_id}] Esecuzione CHUNKING CLASSICO (basato su dimensione).")
                chunks = split_text_into_chunks(article_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            # --- FINE NUOVA LOGICA ---
            if not chunks:
                 logger.info(f"[_index_article][{article_id}] Nessun chunk generato. Marco come completato.")
                 final_status = 'completed'
            else:
                logger.info(f"[_index_article][{article_id}] Creati {len(chunks)} chunk.")
                # Passiamo l'intero core_config, che contiene già tutte le impostazioni necessarie
                embeddings = generate_embeddings(chunks, user_settings=core_config, task_type=TASK_TYPE_DOCUMENT)
                if not embeddings or len(embeddings) != len(chunks):
                    logger.error(f"[_index_article][{article_id}] Fallimento generazione embedding.")
                    final_status = 'failed_embedding'
                else:
                    logger.info(f"[_index_article][{article_id}] Embedding generati.")
                    logger.info(f"[_index_article][{article_id}] Salvataggio in ChromaDB ({article_collection.name})...")
                    ids = [f"{article_id}_chunk_{i}" for i in range(len(chunks))]
                    metadatas_chroma = [{
                        "article_id": article_id, "article_title": title, "article_url": article_url,
                        "chunk_index": i, "source_type": "article",
                        **({"user_id": user_id} if app_mode == 'saas' and user_id else {})
                    } for i in range(len(chunks))]
                    article_collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas_chroma, documents=chunks)
                    logger.info(f"[_index_article][{article_id}] Salvataggio ChromaDB completato.")
                    final_status = 'completed'
    except Exception as e:
        logger.error(f"[_index_article][{article_id}] Errore imprevisto durante indicizzazione: {e}", exc_info=True)
        if final_status not in ['completed', 'failed_embedding']:
             if isinstance(e, FileNotFoundError): final_status = 'failed_file_not_found'
             elif isinstance(e, IOError): final_status = 'failed_reading_file'
             elif 'split_text_into_chunks' in str(e): final_status = 'failed_chunking'
             elif 'upsert' in str(e): final_status = 'failed_chroma_write'
             else: final_status = 'failed_processing_generic'
    
    # ... (Il resto della funzione con le statistiche e l'aggiornamento del DB rimane invariato)
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
            logger.info(f"[_index_article][{article_id}] Statistiche salvate/aggiornate nella cache.")
        except Exception as e_stats:
            logger.error(f"[_index_article][{article_id}] Errore durante il calcolo/salvataggio delle statistiche: {e_stats}")
    try:
        logger.info(f"[_index_article][{article_id}] Aggiornamento stato DB a '{final_status}'...")
        cursor.execute("UPDATE articles SET processing_status = ? WHERE article_id = ?", (final_status, article_id))
    except sqlite3.Error as db_update_err:
         logger.error(f"[_index_article][{article_id}] ERRORE CRITICO aggiornamento stato finale DB: {db_update_err}")
         final_status = 'failed_db_status_update'

    logger.info(f"[_index_article][{article_id}] Indicizzazione terminata con stato restituito: {final_status}")
    return final_status

def _process_rss_feed_core(
    initial_feed_url: str, 
    user_id: Optional[str], 
    core_config: dict, 
    status_dict: Optional[dict] = None,
    status_lock: Optional[threading.Lock] = None
) -> bool:
    """
    Logica centrale per processare TUTTI gli articoli NUOVI di un feed RSS,
    gestendo la paginazione (?paged=X).
    Restituisce True se il processo si completa senza errori critici, False altrimenti.
    """
    logger.info(f"[CORE RSS Process] Avvio per feed={initial_feed_url}, user_id={user_id}")
    overall_success = False
    conn_sqlite = None

    pages_processed = 0
    total_entries = 0
    skipped_count = 0
    saved_ok_count = 0
    failed_count = 0

    try:
        # --- 1. Configurazione e Connessione DB ---
        app_mode = current_app.config.get('APP_MODE', 'single')
        db_path = current_app.config.get('DATABASE_FILE')
        articles_folder = current_app.config.get('ARTICLES_FOLDER_PATH')
        if not db_path or not articles_folder:
            raise RuntimeError("Configurazione DB o Cartella Articoli mancante per processamento core RSS.")

        logger.info(f"[CORE RSS Process] Connessione a DB: {db_path}")
        conn_sqlite = sqlite3.connect(db_path, timeout=10.0)
        conn_sqlite.row_factory = sqlite3.Row
        cursor_sqlite = conn_sqlite.cursor()
        logger.info("[CORE RSS Process] DB connesso.")

        # --- Pre-costruzione mappa URL normalizzate dal DB per confronti veloci ---
        articles_map = {}
        try:
            if app_mode == 'saas' and user_id:
                cursor_sqlite.execute("SELECT article_id, article_url, processing_status FROM articles WHERE user_id = ?", (user_id,))
            else:
                cursor_sqlite.execute("SELECT article_id, article_url, processing_status FROM articles")
            for r in cursor_sqlite.fetchall():
                raw_db_url = r['article_url']
                norm_db_url = normalize_url(raw_db_url) if raw_db_url else raw_db_url
                # In caso di collisioni preferiamo l'ultima letta (non critico)
                articles_map[norm_db_url] = {'article_id': r['article_id'], 'processing_status': r['processing_status']}
            logger.debug(f"[CORE RSS Process] Mappa URL articoli costruita ({len(articles_map)} entries).")
        except Exception as e_map:
            logger.warning(f"[CORE RSS Process] Impossibile costruire mappa URL articoli dal DB: {e_map}")
            articles_map = {}

        # Normalizza URL base per paginazione
        parsed_initial_url = urlparse(initial_feed_url)
        base_feed_url = urljoin(initial_feed_url, parsed_initial_url.path)
        page_number = 1

        # --- 2. CICLO PAGINAZIONE ---
        while True:
            # Costruisci URL pagina
            if page_number == 1:
                url_to_fetch = base_feed_url
            else:
                separator = '&' if '?' in base_feed_url else '?'
                url_to_fetch = f"{base_feed_url}{separator}paged={page_number}"

            # Aggiorna stato
            if status_dict and status_lock:
                with status_lock:
                    status_dict['message'] = f"Analisi pagina #{page_number} del feed..."
                    status_dict['current_page'] = page_number

            logger.info(f"[CORE RSS Process] --- Fetch Pagina #{page_number}: {url_to_fetch} ---")

            parsed_feed = feedparser.parse(url_to_fetch, request_headers={'User-Agent': 'MagazzinoDelCreatoreBot/1.0'}, agent='MagazzinoDelCreatoreBot/1.0')

            # Controllo prima pagina
            if page_number == 1:
                if parsed_feed.bozo:
                    bozo_exception = parsed_feed.get('bozo_exception')
                    is_critical_error = isinstance(bozo_exception, (HTTPError, AttributeError, TypeError, ValueError)) or "document" in str(bozo_exception).lower()
                    if is_critical_error:
                        logger.error(f"[CORE RSS Process] Errore critico parsing prima pagina ({url_to_fetch}): {bozo_exception}. Interruzione.")
                        if conn_sqlite:
                            try: conn_sqlite.close()
                            except: pass
                        return False
                    else:
                        logger.warning(f"[CORE RSS Process] Problema minore parsing prima pagina: {bozo_exception}")

                if not parsed_feed.entries:
                    logger.error("[CORE RSS Process] Nessun articolo sulla prima pagina. Feed vuoto o URL errato?")
                    if conn_sqlite:
                        try: conn_sqlite.close()
                        except: pass
                    return False

            # Fine paginazione e errori per pagine successive
            if parsed_feed.bozo and page_number > 1:
                bozo_exception = parsed_feed.get('bozo_exception')
                if isinstance(bozo_exception, HTTPError) and getattr(bozo_exception, 'code', 0) >= 400:
                    logger.info(f"[CORE RSS Process] Errore HTTP {bozo_exception} pagina {page_number}. Fine paginazione.")
                    break
            if not parsed_feed.entries:
                logger.info(f"[CORE RSS Process] Nessun articolo pagina {page_number}. Fine paginazione.")
                break

            pages_processed += 1
            num_entries_page = len(parsed_feed.entries)
            total_entries += num_entries_page
            logger.info(f"[CORE RSS Process] Trovati {num_entries_page} articoli pagina {page_number}.")

            for entry_index, entry in enumerate(parsed_feed.entries):
                if status_dict and status_lock:
                    with status_lock:
                        status_dict['message'] = f"Articolo {entry_index + 1}/{num_entries_page} (Pag. {page_number}): {entry.get('title', 'Senza Titolo')[:50]}..."
                        status_dict['total_articles_processed'] = saved_ok_count + failed_count + skipped_count
                        status_dict['page_total_articles'] = num_entries_page
                        status_dict['page_processed_articles'] = entry_index + 1

                logger.debug(f"[CORE RSS Process] Pag.{page_number}, Art.{entry_index+1}: Processo entry...")
                article_url = entry.get('link')
                title = entry.get('title', 'Senza Titolo')
                content_filepath = None
                article_id_to_process = None
                needs_processing = False

                if not article_url:
                    logger.warning(f"[CORE RSS Process] Articolo '{title}' saltato: manca URL.")
                    skipped_count += 1
                    continue

                # Normalizza URL per confronto
                norm_article_url = normalize_url(article_url)

                # Controllo esistenza tramite mappa normalizzata
                existing = articles_map.get(norm_article_url)

                if existing:
                    if existing['processing_status'] == 'pending' or (existing['processing_status'] and str(existing['processing_status']).startswith('failed_')):
                        article_id_to_process = existing['article_id']
                        needs_processing = True
                        logger.info(f"[CORE RSS Process] Riprovocesso articolo esistente: {title} (matched by normalized URL)")
                    else:
                        skipped_count += 1
                        logger.debug(f"[CORE RSS Process] Articolo già processato/in corso: {title}")
                        continue
                else:
                    # Nuovo articolo
                    article_id_to_process = str(uuid.uuid4())
                    needs_processing = True
                    logger.info(f"[CORE RSS Process] Nuovo articolo: {title}")
                    guid = entry.get('id') or entry.get('guid') or article_url
                    published_at_iso = parse_feed_date(entry.get('published_parsed') or entry.get('updated_parsed'))

                    # Estrazione del contenuto (priorità: content > summary > fetch full article)
                    content = None
                    try:
                        if 'content' in entry and entry.get('content'):
                            content_data = entry.content[0]
                            soup_content = BeautifulSoup(content_data.value, 'html.parser')
                            content = '\n'.join(line.strip() for line in soup_content.get_text(separator='\n', strip=True).splitlines() if line.strip())
                        elif 'summary' in entry and entry.get('summary'):
                            soup_summary = BeautifulSoup(entry.summary, 'html.parser')
                            content = '\n'.join(line.strip() for line in soup_summary.get_text(separator='\n', strip=True).splitlines() if line.strip())
                            full_content = get_full_article_content(article_url)
                            content = full_content if full_content else content
                        else:
                            content = get_full_article_content(article_url)
                    except Exception as e_content:
                        logger.warning(f"[CORE RSS Process] Errore estrazione contenuto per '{title}': {e_content}")
                        content = None

                    if not content:
                        logger.warning(f"[CORE RSS Process] No content per '{title}'. Articolo saltato.")
                        skipped_count += 1
                        needs_processing = False
                        continue

                    # Inserimento nel DB e aggiornamento mappa per evitare duplicati nella stessa run
                    try:
                        cursor_sqlite.execute("""
                            INSERT INTO articles (article_id, guid, feed_url, article_url, title, published_at, content, user_id, processing_status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (article_id_to_process, guid, initial_feed_url, article_url, title, published_at_iso, content, user_id, 'pending'))
                        # aggiorna mappa in memoria
                        articles_map[norm_article_url] = {'article_id': article_id_to_process, 'processing_status': 'pending'}
                        logger.info(f"[CORE RSS Process] Inserito nuovo '{title}'.")
                    except Exception as e_save:
                        logger.error(f"[CORE RSS Process] Errore save/insert '{title}': {e_save}", exc_info=True)
                        skipped_count += 1
                        needs_processing = False
                        # non rimuoviamo file (non usiamo file system per articoli in questa versione)
                        continue

                # Indicizzazione (se serve) - _index_article aggiorna lo stato nel DB ma non fa commit
                if needs_processing and article_id_to_process:
                    logger.info(f"[CORE RSS Process] Indicizzazione articolo {article_id_to_process} ('{title}')...")
                    try:
                        indexing_status = _index_article(article_id_to_process, conn_sqlite, user_id, core_config)
                        if indexing_status == 'completed':
                            saved_ok_count += 1
                            # aggiorna mappa per riflettere stato finale
                            articles_map[norm_article_url]['processing_status'] = 'completed'
                        else:
                            failed_count += 1
                            articles_map[norm_article_url]['processing_status'] = indexing_status
                    except Exception as e_index:
                        logger.error(f"[CORE RSS Process] Errore indicizzazione per '{title}': {e_index}", exc_info=True)
                        failed_count += 1
                        articles_map[norm_article_url] = {'article_id': article_id_to_process, 'processing_status': 'failed_processing_generic'}

            # Fine ciclo articoli pagina -> avanza pagina
            page_number += 1

        # Commit finale (IMPORTANTE)
        logger.info("[CORE RSS Process] Esecuzione COMMIT finale DB...")
        conn_sqlite.commit()
        logger.info("[CORE RSS Process] COMMIT DB eseguito.")
        overall_success = True

    except sqlite3.Error as e_sql_outer:
        logger.error(f"[CORE RSS Process] Errore SQLite esterno: {e_sql_outer}", exc_info=True)
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    except RuntimeError as rte:
        logger.error(f"[CORE RSS Process] Errore runtime: {rte}", exc_info=True)
        overall_success = False
    except Exception as e_core_generic:
        logger.exception(f"[CORE RSS Process] Errore generico imprevisto: {e_core_generic}")
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    finally:
        if conn_sqlite:
            try:
                conn_sqlite.close()
                logger.info("[CORE RSS Process] Connessione SQLite chiusa.")
            except Exception as close_err:
                logger.error(f"[CORE RSS Process] Errore chiusura DB: {close_err}")

    log_summary = (
        f"[CORE RSS Process] Riepilogo per feed {initial_feed_url} (Utente: {user_id}): "
        f"Pagine:{pages_processed}, Tot Articoli Feed:{total_entries}, "
        f"Processati/Riprovati:{saved_ok_count + failed_count} (OK:{saved_ok_count}, Fail:{failed_count}), "
        f"Saltati:{skipped_count}. Successo Generale: {overall_success}"
    )
    if overall_success:
        logger.info(log_summary)
    else:
        logger.error(log_summary)

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

# --- Endpoint Principale (ORA ASINCRONO) ---
@rss_bp.route('/process', methods=['POST'])
@login_required
def process_rss_feed():
    """
    AVVIA l'elaborazione di un feed RSS (con paginazione) in background.
    Restituisce 202 Accepted.
    """
    global rss_processing_status, rss_status_lock
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"Richiesta AVVIO processo feed RSS (Modalità: {app_mode}).")

    current_user_id = current_user.id if app_mode == 'saas' and current_user.is_authenticated else None
    if app_mode == 'saas' and not current_user_id: 
        return jsonify({'success': False, 'error_code': 'AUTH_REQUIRED'}), 401

    with rss_status_lock:
        if rss_processing_status.get('is_processing', False):
            logger.warning("Tentativo avvio processo RSS mentre un altro è attivo.")
            return jsonify({'success': False, 'error_code': 'ALREADY_PROCESSING', 'message': 'Un processo RSS è già attivo.'}), 409

        # --- MODIFICA CHIAVE: IMPOSTIAMO LO STATO PRIMA DI RISPONDERE ---
        rss_processing_status.update({
            'is_processing': True,
            'current_page': 0,
            'total_articles_processed': 0,
            'message': 'Avvio elaborazione feed in background...',
            'error': None
        })
        # ----------------------------------------------------------------

    if not request.is_json: 
        with rss_status_lock: rss_processing_status['is_processing'] = False # Resetta in caso di errore
        return jsonify({'success': False, 'error_code': 'INVALID_CONTENT_TYPE', 'message': 'La richiesta deve essere JSON.'}), 400
    
    data = request.get_json()
    initial_feed_url = data.get('rss_url')
    
    if not initial_feed_url or not is_valid_url(initial_feed_url):
        with rss_status_lock: rss_processing_status['is_processing'] = False # Resetta in caso di errore
        error_message = "Parametro 'rss_url' mancante." if not initial_feed_url else f"L'URL fornito '{initial_feed_url}' non e un URL valido."
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': error_message}), 400

    try:
        app_context = current_app.app_context()
        # Passiamo una copia dello stato iniziale, ma lo stato globale è già impostato
        background_thread = threading.Thread(
            target=_background_rss_processing,
            args=(app_context, initial_feed_url, current_user_id, copy.deepcopy(rss_processing_status))
        )
        background_thread.daemon = True
        background_thread.start()
        logger.info(f"Thread background RSS avviato per: {initial_feed_url}")

        return jsonify({
            'success': True,
            'message': 'Elaborazione feed avviata in background. Controlla lo stato periodicamente.'
        }), 202

    except Exception as e_start:
        logger.exception("Errore CRITICO avvio thread RSS.")
        with rss_status_lock:
            rss_processing_status['is_processing'] = False
            rss_processing_status['message'] = f"Errore avvio processo: {e_start}"
            rss_processing_status['error'] = str(e_start)
        return jsonify({'success': False, 'error_code': 'THREAD_START_FAILED', 'message': f'Errore avvio processo background: {e_start}'}), 500
    indexing_logic.py
    
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
    app_mode = current_app.config.get('APP_MODE', 'single')
    db_path = current_app.config.get('DATABASE_FILE')
    logger.info(f"Richiesta download contenuto articoli (Modalità: {app_mode})")

    current_user_id = None
    if app_mode == 'saas':
        if not current_user.is_authenticated: return jsonify({'success': False, 'error_code': 'AUTH_REQUIRED'}), 401
        current_user_id = current_user.id
        logger.info(f"Download articoli per utente '{current_user_id}'")

    if not db_path:
        logger.error("Percorso DATABASE_FILE non configurato per download articoli.")
        return jsonify({'success': False, 'error': 'Server configuration error.'}), 500

    all_articles_content = io.StringIO()
    conn = None
    articles_processed_count = 0
    articles_read_errors = []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Ora leggiamo direttamente la colonna 'content'
        sql_query = "SELECT article_id, title, article_url, content FROM articles WHERE processing_status = 'completed' AND content IS NOT NULL"
        params = []
        if app_mode == 'saas':
           sql_query += " AND user_id = ?"
           params.append(current_user_id)
        sql_query += " ORDER BY published_at DESC"

        cursor.execute(sql_query, tuple(params))

        for row in cursor.fetchall():
            content = row['content']
            if content:
                articles_processed_count += 1
                all_articles_content.write(f"--- ARTICOLO START ---\n")
                all_articles_content.write(f"ID: {row['article_id']}\n")
                all_articles_content.write(f"Titolo: {row['title']}\n")
                all_articles_content.write(f"URL: {row['article_url']}\n")
                all_articles_content.write(f"--- Contenuto ---\n{content}\n")
                all_articles_content.write(f"--- ARTICOLO END ---\n\n\n")
            else:
                articles_read_errors.append(row['article_id'])
        
        conn.close()
        logger.info(f"Recuperati contenuti da {articles_processed_count} articoli. Errori lettura: {len(articles_read_errors)}.")

    except sqlite3.Error as e:
        logger.error(f"Errore DB durante recupero articoli per download: {e}")
        if conn: conn.close()
        return jsonify({'success': False, 'error': 'Database error retrieving articles.'}), 500
    except Exception as e_outer:
         logger.error(f"Errore generico durante preparazione download: {e_outer}", exc_info=True)
         if conn: conn.close()
         return jsonify({'success': False, 'error': 'Unexpected error during download preparation.'}), 500

    output_filename = f"articles_{current_user_id if app_mode=='saas' else 'all'}.txt"
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
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"Richiesta DELETE /api/rss/all ricevuta (Modalità: {app_mode})")

    if app_mode != 'saas':
        return jsonify({'success': False, 'error_code': 'INVALID_MODE', 'message': 'Operazione permessa solo in modalità SAAS.'}), 403

    current_user_id = current_user.id
    logger.info(f"Avvio eliminazione di massa articoli per utente: {current_user_id}")

    db_path = current_app.config.get('DATABASE_FILE')
    base_article_collection_name = current_app.config.get('ARTICLE_COLLECTION_NAME', 'article_content')
    chroma_client = current_app.config.get('CHROMA_CLIENT')

    if not db_path or not base_article_collection_name or not chroma_client:
         return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Errore configurazione server.'}), 500

    user_article_collection_name = f"{base_article_collection_name}_{current_user_id}"
    conn_sqlite = None
    sqlite_rows_affected = 0
    sqlite_rows_after_delete = -1
    chroma_deleted = False
    chroma_error = None

    try:
        # 1. Elimina da SQLite
        conn_sqlite = sqlite3.connect(db_path)
        cursor_sqlite = conn_sqlite.cursor()

        cursor_sqlite.execute("DELETE FROM articles WHERE user_id = ?", (current_user_id,))
        sqlite_rows_affected = cursor_sqlite.rowcount
        
        cursor_sqlite.execute("DELETE FROM content_stats WHERE user_id = ? AND source_type = 'articles'", (current_user_id,))
        conn_sqlite.commit()
        
        cursor_sqlite.execute("SELECT COUNT(*) FROM articles WHERE user_id = ?", (current_user_id,))
        sqlite_rows_after_delete = cursor_sqlite.fetchone()[0]

    except sqlite3.Error as e_sql:
        logger.error(f"[{current_user_id}] Errore SQLite durante eliminazione articoli: {e_sql}", exc_info=True)
        if conn_sqlite: conn_sqlite.rollback()
        return jsonify({'success': False, 'error_code': 'DB_DELETE_FAILED', 'message': f'Errore DB durante eliminazione: {e_sql}'}), 500
    finally:
         if conn_sqlite: conn_sqlite.close()

    # 2. Elimina Collezione ChromaDB
    try:
        chroma_client.delete_collection(name=user_article_collection_name)
        chroma_deleted = True
    except Exception as e_chroma:
        logger.warning(f"[{current_user_id}] Errore/avviso durante eliminazione collezione ChromaDB '{user_article_collection_name}': {e_chroma}")
        chroma_deleted = False
        chroma_error = str(e_chroma)

    # 3. Risposta Finale
    final_success = (sqlite_rows_after_delete == 0)
    message = f"Eliminazione articoli utente {current_user_id} completata. Record SQLite affetti: {sqlite_rows_affected}."
    
    return jsonify({
        'success': final_success,
        'message': message,
    }), 200 if final_success else 500

@rss_bp.route('/debug_summary', methods=['GET'])
@login_required
def debug_rss_summary():
    """
    Endpoint diagnostico: restituisce conteggi e alcuni esempi dagli articoli
    (utile per capire discrepanze DB <> UI) e informazioni sulla collezione Chroma.
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    db_path = current_app.config.get('DATABASE_FILE')
    current_user_id = current_user.id if app_mode == 'saas' and current_user.is_authenticated else None

    result = {
        'app_mode': app_mode,
        'user_id': current_user_id,
        'db': {},
        'sample_articles': [],
        'chroma': {}
    }

    # 1) Statistiche DB
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Totale articoli (eventualmente filtrati per utente)
        total_query = "SELECT COUNT(*) as c FROM articles"
        params = ()
        if app_mode == 'saas':
            total_query += " WHERE user_id = ?"
            params = (current_user_id,)
        cur.execute(total_query, params)
        result['db']['total_articles'] = cur.fetchone()['c']

        # Conteggio per stato di processing_status
        status_query = "SELECT processing_status, COUNT(*) as c FROM articles"
        if app_mode == 'saas':
            status_query += " WHERE user_id = ?"
        status_query += " GROUP BY processing_status"
        cur.execute(status_query, params)
        status_counts = {row['processing_status'] if row['processing_status'] else 'NULL': row['c'] for row in cur.fetchall()}
        result['db']['by_status'] = status_counts

        # Ultimi 10 articoli per ispezione
        sample_query = "SELECT article_id, title, article_url, processing_status, added_at FROM articles"
        if app_mode == 'saas':
            sample_query += " WHERE user_id = ?"
        sample_query += " ORDER BY added_at DESC LIMIT 10"
        cur.execute(sample_query, params)
        for r in cur.fetchall():
            result['sample_articles'].append({
                'article_id': r['article_id'],
                'title': r['title'],
                'url': r['article_url'],
                'status': r['processing_status'],
                'added_at': r['added_at']
            })

    except Exception as e:
        result['db']['error'] = str(e)
    finally:
        if conn:
            conn.close()

    # 2) Info Collezione Chroma (conta documenti, nome collezione usato)
    try:
        chroma_client = current_app.config.get('CHROMA_CLIENT')
        base_name = current_app.config.get('ARTICLE_COLLECTION_NAME', 'article_content')
        collection_name = f"{base_name}_{current_user_id}" if app_mode == 'saas' and current_user_id else base_name
        result['chroma']['collection_name'] = collection_name

        if not chroma_client:
            result['chroma']['error'] = 'Chroma client non configurato'
        else:
            try:
                coll = chroma_client.get_collection(name=collection_name)
                # Proviamo metodi diversi in modo robusto
                count = None
                if hasattr(coll, 'count'):
                    try:
                        count = coll.count()
                    except Exception:
                        count = None
                if count is None:
                    try:
                        all_res = coll.get()  # ATTENZIONE: può restituire molti id, è solo diagnostico
                        ids = all_res.get('ids', [])
                        count = len(ids)
                    except Exception as e_get:
                        # fallback: prova a ottenere una piccola porzione per capire se la collezione risponde
                        try:
                            small = coll.get(limit=1)
                            count = small.get('metadata', {}).get('count', None) if isinstance(small, dict) else None
                        except Exception as e_small:
                            result['chroma']['get_error'] = f"{e_get} | {e_small}"
                result['chroma']['document_count'] = count
            except Exception as e_coll:
                result['chroma']['error'] = str(e_coll)
    except Exception as e:
        result['chroma']['error'] = str(e)

    return jsonify(result), 200
