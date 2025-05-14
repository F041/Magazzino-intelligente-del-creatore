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
from flask import Blueprint, request, jsonify, current_app
from google.api_core import exceptions as google_exceptions
import threading
import copy

try:
    from app.services.embedding.gemini_embedding import split_text_into_chunks, get_gemini_embeddings, TASK_TYPE_DOCUMENT
except ImportError:
    # ... (gestione errore import) ...
    logger.error("!!! Impossibile importare funzioni di embedding/chunking (rss.py) !!!")
    split_text_into_chunks = None
    get_gemini_embeddings = None
    TASK_TYPE_DOCUMENT = "retrieval_document"


logger = logging.getLogger(__name__)
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
        response = requests.get(article_url, headers=headers, timeout=10) # Timeout
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

def _index_article(article_id: str, conn: sqlite3.Connection, user_id: Optional[str] = None) -> str:
    """
    Esegue l'indicizzazione di un articolo (legge contenuto, chunk, embed, Chroma).
    Gestisce modalità 'single' e 'saas'.
    NON fa commit; si aspetta che il chiamante gestisca la transazione.
    Restituisce lo stato finale ('completed' o 'failed_...').
    """
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"[_index_article][{article_id}] Avvio indicizzazione (Modalità: {app_mode}, UserID: {user_id if user_id else 'N/A'})")

    final_status = 'failed_indexing_init'
    cursor = conn.cursor()

    # Recupera Configurazione Essenziale
    llm_api_key = current_app.config.get('GOOGLE_API_KEY')
    embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
    chunk_size = current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS', 300)
    chunk_overlap = current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS', 50)
    base_article_collection_name = current_app.config.get('ARTICLE_COLLECTION_NAME', 'article_content') # Nome base

    # Ottieni Collezione ChromaDB
    article_collection = None
    collection_name_for_log = "N/A"

    if app_mode == 'single':
        article_collection = current_app.config.get('CHROMA_ARTICLE_COLLECTION')
        if article_collection: collection_name_for_log = article_collection.name
        else: logger.error(f"[_index_article][{article_id}] Modalità SINGLE: Collezione articoli non trovata!"); return 'failed_config_collection_missing'
    elif app_mode == 'saas':
        if not user_id: logger.error(f"[_index_article][{article_id}] Modalità SAAS: User ID mancante!"); return 'failed_user_id_missing'
        chroma_client = current_app.config.get('CHROMA_CLIENT')
        if not chroma_client: logger.error(f"[_index_article][{article_id}] Modalità SAAS: Chroma Client non trovato!"); return 'failed_config_client_missing'

        user_article_collection_name = f"{base_article_collection_name}_{user_id}"
        collection_name_for_log = user_article_collection_name
        try:
            logger.info(f"[_index_article][{article_id}] Modalità SAAS: Ottenimento/Creazione collezione '{user_article_collection_name}'...")
            article_collection = chroma_client.get_or_create_collection(name=user_article_collection_name)
        except Exception as e_saas_coll: logger.error(f"[_index_article][{article_id}] Modalità SAAS: Errore get/create collezione '{user_article_collection_name}': {e_saas_coll}"); return 'failed_chroma_collection_saas'
    else: logger.error(f"[_index_article][{article_id}] Modalità APP non valida: {app_mode}"); return 'failed_invalid_mode'

    if not article_collection: logger.error(f"[_index_article][{article_id}] Fallimento ottenimento collezione ChromaDB (Nome tentato: {collection_name_for_log})."); return 'failed_chroma_collection_generic'
    logger.info(f"[_index_article][{article_id}] Collezione Chroma '{collection_name_for_log}' pronta.")

    if not llm_api_key or not embedding_model: logger.error(f"[_index_article][{article_id}] Configurazione Embedding mancante."); return 'failed_config_embedding'
    if not split_text_into_chunks or not get_gemini_embeddings: logger.error(f"[_index_article][{article_id}] Funzioni chunk/embed non disponibili."); return 'failed_server_setup'

    # Logica Indicizzazione
    try:
        # 1. Recupera dati articolo (invariato)
        cursor.execute("SELECT extracted_content_path, title, article_url FROM articles WHERE article_id = ?", (article_id,))
        article_data = cursor.fetchone()
        if not article_data: logger.error(f"[_index_article][{article_id}] Record non trovato nel DB."); return 'failed_article_not_found'
        content_filepath, title, article_url = article_data
        logger.info(f"[_index_article][{article_id}] Trovato: '{title}', File: {content_filepath}")

        # 2. Leggi contenuto dal file salvato
        article_content = None
        if not content_filepath or not os.path.exists(content_filepath):
             logger.error(f"[_index_article][{article_id}] File contenuto non trovato: {content_filepath}")
             return 'failed_file_not_found'
        with open(content_filepath, 'r', encoding='utf-8') as f:
            article_content = f.read()
        if not article_content or not article_content.strip():
             logger.warning(f"[_index_article][{article_id}] File contenuto vuoto. Marco come completato.")
             # IMPORTANTE: Rimuoviamo il return 'completed' qui. Lo stato verrà impostato
             # dopo aver tentato l'aggiornamento del DB.
             final_status = 'completed'
        else: # Se c'è contenuto, procedi con chunking/embedding
            logger.info(f"[_index_article][{article_id}] Contenuto letto ({len(article_content)} chars).")

            # 3. Chunking
            chunks = split_text_into_chunks(article_content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            if not chunks:
                 logger.info(f"[_index_article][{article_id}] Nessun chunk generato. Marco come completato.")
                 final_status = 'completed'
            else:
                logger.info(f"[_index_article][{article_id}] Creati {len(chunks)} chunk.")

                # 4. Embedding
                embeddings = get_gemini_embeddings(chunks, api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_DOCUMENT)
                if not embeddings or len(embeddings) != len(chunks):
                    logger.error(f"[_index_article][{article_id}] Fallimento generazione embedding.")
                    final_status = 'failed_embedding'
                else:
                    logger.info(f"[_index_article][{article_id}] Embedding generati.")

                    # 5. Salvataggio in ChromaDB
                    logger.info(f"[_index_article][{article_id}] Salvataggio in ChromaDB ({article_collection.name})...")
                    ids = [f"{article_id}_chunk_{i}" for i in range(len(chunks))]
                    metadatas_chroma = [{
                        "article_id": article_id, "article_title": title, "article_url": article_url,
                        "chunk_index": i, "source_type": "article",
                        **({"user_id": user_id} if app_mode == 'saas' and user_id else {}) # Aggiunge user_id se saas
                    } for i in range(len(chunks))]
                    article_collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas_chroma, documents=chunks)
                    logger.info(f"[_index_article][{article_id}] Salvataggio ChromaDB completato.")
                    final_status = 'completed' # Successo!

    except Exception as e:
        logger.error(f"[_index_article][{article_id}] Errore imprevisto durante indicizzazione: {e}", exc_info=True)
        # Aggiorniamo final_status solo se non è già 'completed' o 'failed_embedding'
        if final_status not in ['completed', 'failed_embedding']:
             if isinstance(e, FileNotFoundError): final_status = 'failed_file_not_found'
             elif isinstance(e, IOError): final_status = 'failed_reading_file'
             elif 'split_text_into_chunks' in str(e): final_status = 'failed_chunking'
             elif 'upsert' in str(e): final_status = 'failed_chroma_write'
             else: final_status = 'failed_processing_generic'

    # Aggiorna stato DB (NON fa commit)
    try:
        logger.info(f"[_index_article][{article_id}] Aggiornamento stato DB a '{final_status}'...")
        # Riga corretta:
        cursor.execute("UPDATE articles SET processing_status = ? WHERE article_id = ?", (final_status, article_id))
    except sqlite3.Error as db_update_err:
         logger.error(f"[_index_article][{article_id}] ERRORE CRITICO aggiornamento stato finale DB: {db_update_err}")
         final_status = 'failed_db_status_update'

    logger.info(f"[_index_article][{article_id}] Indicizzazione terminata con stato restituito: {final_status}")
    return final_status

def _process_rss_feed_core(initial_feed_url: str, user_id: Optional[str], core_config: dict) -> bool:
    """
    Logica centrale per processare TUTTI gli articoli NUOVI di un feed RSS,
    gestendo la paginazione (?paged=X).
    Recupera articoli, confronta con DB (filtrando per user_id se SAAS),
    processa i nuovi (salva file, indicizza in Chroma), salva nel DB.
    Richiede un contesto applicativo Flask attivo per current_app.config.
    Restituisce True se il processo si completa senza errori critici, False altrimenti.
    """
    logger.info(f"[CORE RSS Process] Avvio per feed={initial_feed_url}, user_id={user_id}")
    overall_success = False
    conn_sqlite = None
    # Contatori specifici
    pages_processed = 0; total_entries = 0; skipped_count = 0; saved_ok_count = 0; failed_count = 0;

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

        # Normalizza URL base
        parsed_initial_url = urlparse(initial_feed_url)
        base_feed_url = urljoin(initial_feed_url, parsed_initial_url.path)
        page_number = 1

        # --- 2. CICLO PAGINAZIONE ---
        while True:
            # Costruisci URL pagina
            if page_number == 1:
                url_to_fetch = base_feed_url
            else:
                separator = '&' if '?' in base_feed_url else '?'; url_to_fetch = f"{base_feed_url}{separator}paged={page_number}"
            logger.info(f"[CORE RSS Process] --- Fetch Pagina #{page_number}: {url_to_fetch} ---")

            # === ESEGUI IL PARSING QUI ===
            parsed_feed = feedparser.parse(url_to_fetch, request_headers={'User-Agent': 'MagazzinoDelCreatoreBot/1.0'}, agent='MagazzinoDelCreatoreBot/1.0')

            # === SPOSTA IL CONTROLLO DELLA PRIMA PAGINA QUI (DOPO IL PARSING) ===
            if page_number == 1:
                if parsed_feed.bozo:
                    bozo_exception = parsed_feed.get('bozo_exception')
                    # Verifica se è un errore grave che impedisce la lettura
                    is_critical_error = isinstance(bozo_exception, (HTTPError, AttributeError, TypeError, ValueError)) or "document" in str(bozo_exception).lower() # Aggiungi controlli se necessario
                    if is_critical_error: # Controlla solo errori gravi sulla prima pagina
                        logger.error(f"[CORE RSS Process] Errore critico parsing prima pagina ({url_to_fetch}): {bozo_exception}. Interruzione.");
                        if conn_sqlite:
                            try:
                                conn_sqlite.close()
                            except:
                                pass
                        return False # Fallimento se la prima pagina è inaccessibile/invalida
                    else: # Warning per errori minori (es. non well-formed ma leggibile)
                        logger.warning(f"[CORE RSS Process] Problema minore parsing prima pagina: {bozo_exception}")

                if not parsed_feed.entries:
                    logger.error("[CORE RSS Process] Nessun articolo sulla prima pagina. Feed vuoto o URL errato?");
                    if conn_sqlite:
                        try:
                            conn_sqlite.close()
                        except:
                            pass
                    return False # Fallimento se la prima pagina è vuota
            # === FINE BLOCCO SPOSTATO ===

            # Controllo Errori Generico / Fine Paginazione (per pagine successive alla prima)
            if parsed_feed.bozo and page_number > 1: # Controlla bozo solo da pagina 2 in poi per errori HTTP
                 bozo_exception = parsed_feed.get('bozo_exception')
                 if isinstance(bozo_exception, HTTPError) and bozo_exception.code >= 400:
                    logger.info(f"[CORE RSS Process] Errore HTTP {bozo_exception.code} pagina {page_number}. Fine paginazione."); break
            if not parsed_feed.entries:
                logger.info(f"[CORE RSS Process] Nessun articolo pagina {page_number}. Fine paginazione.");
                break

            # Processamento Articoli Pagina
            pages_processed += 1
            num_entries_page = len(parsed_feed.entries)
            total_entries += num_entries_page
            logger.info(f"[CORE RSS Process] Trovati {num_entries_page} articoli pagina {page_number}.")

            for entry_index, entry in enumerate(parsed_feed.entries):
                logger.debug(f"[CORE RSS Process] Pag.{page_number}, Art.{entry_index+1}: Processo entry...")
                article_url = entry.get('link'); title = entry.get('title', 'Senza Titolo')
                content_filepath = None; article_id_to_process = None; needs_processing = False

                if not article_url: logger.warning(f"[CORE RSS Process] Articolo '{title}' saltato: manca URL."); skipped_count+=1; continue

                # Controllo Esistenza (filtrato per utente se SAAS)
                sql_check = "SELECT article_id, processing_status FROM articles WHERE article_url = ?"
                params_check = [article_url]
                if app_mode == 'saas' and user_id: sql_check += " AND user_id = ?"; params_check.append(user_id)
                cursor_sqlite.execute(sql_check, tuple(params_check))
                existing = cursor_sqlite.fetchone()

                if existing:
                    if existing['processing_status'] == 'pending' or existing['processing_status'].startswith('failed_'):
                        article_id_to_process = existing['article_id']; needs_processing = True; logger.info(f"[CORE RSS Process] Riprovocesso articolo esistente: {title}")
                    else: skipped_count+=1; logger.debug(f"[CORE RSS Process] Articolo già processato/in corso: {title}"); continue
                else: # Nuovo
                    article_id_to_process = str(uuid.uuid4()); needs_processing = True; logger.info(f"[CORE RSS Process] Nuovo articolo: {title}")
                    guid = entry.get('id') or entry.get('guid') or article_url
                    published_at_iso = parse_feed_date(entry.get('published_parsed') or entry.get('updated_parsed'))
                    content = None # ... (Logica estrazione content come prima) ...
                    if 'content' in entry and entry.content: content_data = entry.content[0]; soup_content = BeautifulSoup(content_data.value, 'html.parser'); content = '\n'.join(line.strip() for line in soup_content.get_text(separator='\n', strip=True).splitlines() if line.strip())
                    elif 'summary' in entry and entry.summary: soup_summary = BeautifulSoup(entry.summary, 'html.parser'); content = '\n'.join(line.strip() for line in soup_summary.get_text(separator='\n', strip=True).splitlines() if line.strip()); full_content = get_full_article_content(article_url); content = full_content if full_content else content
                    else: content = get_full_article_content(article_url)

                    if not content: logger.warning(f"[CORE RSS Process] No content '{title}'."); skipped_count+=1; needs_processing = False; continue

                    content_filename = f"{article_id_to_process}.txt"; content_filepath = os.path.join(articles_folder, content_filename)
                    try:
                        with open(content_filepath, 'w', encoding='utf-8') as f_content: f_content.write(content)
                        cursor_sqlite.execute("""
                            INSERT INTO articles (article_id, guid, feed_url, article_url, title, published_at, extracted_content_path, user_id, processing_status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (article_id_to_process, guid, initial_feed_url, article_url, title, published_at_iso, content_filepath, user_id, 'pending'))
                        logger.info(f"[CORE RSS Process] Inserito nuovo '{title}'.")
                    except Exception as e_save:
                        logger.error(f"[CORE RSS Process] Errore save/insert '{title}': {e_save}", exc_info=True); skipped_count+=1; needs_processing = False;
                        # Blocco try-except corretto per la rimozione del file
                        if content_filepath and os.path.exists(content_filepath):
                            try:
                                os.remove(content_filepath)
                                logger.info(f"[CORE RSS Process] File parziale rimosso: {content_filepath}")
                            except OSError as remove_error:
                                # Logga l'errore ma continua comunque
                                logger.warning(f"[CORE RSS Process] Impossibile rimuovere file parziale {content_filepath}: {remove_error}")
                                pass # Ignora l'errore di rimozione
                        continue # Continua con il prossimo articolo

                # Indicizzazione (se serve)
                if needs_processing and article_id_to_process:
                    logger.info(f"[CORE RSS Process] Indicizzazione articolo {article_id_to_process} ('{title}')...")
                    # _index_article aggiorna lo stato nel DB ma non fa commit
                    indexing_status = _index_article(article_id_to_process, conn_sqlite, user_id)
                    if indexing_status == 'completed': saved_ok_count += 1
                    else: failed_count += 1
            # Fine ciclo articoli pagina
            page_number += 1 # Vai alla prossima pagina
        # --- FINE CICLO PAGINAZIONE ---

        # Commit finale (IMPORTANTE!)
        logger.info("[CORE RSS Process] Esecuzione COMMIT finale DB...")
        conn_sqlite.commit()
        logger.info("[CORE RSS Process] COMMIT DB eseguito.")
        overall_success = True # Se siamo arrivati qui senza eccezioni gravi, è un successo

    except sqlite3.Error as e_sql_outer:
        logger.error(f"[CORE RSS Process] Errore SQLite esterno: {e_sql_outer}", exc_info=True)
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    except RuntimeError as rte: # Es. errore config
         logger.error(f"[CORE RSS Process] Errore runtime: {rte}", exc_info=True)
         overall_success = False
    except Exception as e_core_generic:
        logger.exception(f"[CORE RSS Process] Errore generico imprevisto.")
        if conn_sqlite: conn_sqlite.rollback()
        overall_success = False
    finally:
        if conn_sqlite:
            try: conn_sqlite.close(); logger.info("[CORE RSS Process] Connessione SQLite chiusa.")
            except Exception as close_err: logger.error(f"[CORE RSS Process] Errore chiusura DB: {close_err}")

    log_summary = (f"[CORE RSS Process] Riepilogo per feed {initial_feed_url} (Utente: {user_id}): "
                   f"Pagine:{pages_processed}, Tot Articoli Feed:{total_entries}, "
                   f"Processati/Riprovati:{saved_ok_count + failed_count} (OK:{saved_ok_count}, Fail:{failed_count}), "
                   f"Saltati:{skipped_count}. Successo Generale: {overall_success}")
    if overall_success: logger.info(log_summary)
    else: logger.error(log_summary)

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
            # === COSTRUISCI IL DIZIONARIO core_config QUI ===
            # Simile a come fatto in scheduler_jobs.py e videos.py
            core_config_dict = {
                'APP_MODE': current_app.config.get('APP_MODE', 'single'),
                'DATABASE_FILE': current_app.config.get('DATABASE_FILE'),
                'ARTICLES_FOLDER_PATH': current_app.config.get('ARTICLES_FOLDER_PATH'),
                'GOOGLE_API_KEY': current_app.config.get('GOOGLE_API_KEY'), # Per embedding
                'GEMINI_EMBEDDING_MODEL': current_app.config.get('GEMINI_EMBEDDING_MODEL'),
                'DEFAULT_CHUNK_SIZE_WORDS': current_app.config.get('DEFAULT_CHUNK_SIZE_WORDS'),
                'DEFAULT_CHUNK_OVERLAP_WORDS': current_app.config.get('DEFAULT_CHUNK_OVERLAP_WORDS'),
                'CHROMA_CLIENT': current_app.config.get('CHROMA_CLIENT'),
                'ARTICLE_COLLECTION_NAME': current_app.config.get('ARTICLE_COLLECTION_NAME'),
                # Aggiungi altre chiavi se _process_rss_feed_core o _index_article ne richiedono altre
            }
            # Verifica che i valori essenziali non siano None (opzionale ma buona pratica)
            required_keys_rss = ['DATABASE_FILE', 'ARTICLES_FOLDER_PATH', 'GOOGLE_API_KEY', 'CHROMA_CLIENT']
            missing_keys_rss = [k for k in required_keys_rss if not core_config_dict.get(k)]
            if missing_keys_rss:
                 raise RuntimeError(f"Valori mancanti nella config per il thread RSS: {', '.join(missing_keys_rss)}")
            logger.info("BACKGROUND THREAD RSS: Dizionario 'core_config_dict' preparato.")
            # === FINE COSTRUZIONE core_config ===

            # --- Chiama la Funzione Core PASSANDO core_config_dict ---
            job_success = _process_rss_feed_core(initial_feed_url, user_id, core_config_dict) # <--- PASSALO QUI

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
    global rss_processing_status, rss_status_lock # Riferimenti globali
    app_mode = current_app.config.get('APP_MODE', 'single')
    logger.info(f"Richiesta AVVIO processo feed RSS (Modalità: {app_mode}).")

    # --- Ottieni User ID ---
    current_user_id = current_user.id if app_mode == 'saas' and current_user.is_authenticated else None
    if app_mode == 'saas' and not current_user_id: return jsonify({'success': False, 'error_code': 'AUTH_REQUIRED'}), 401

    # --- Controlla se Già in Esecuzione ---
    with rss_status_lock:
        if rss_processing_status.get('is_processing', False):
            logger.warning("Tentativo avvio processo RSS mentre un altro è attivo.")
            return jsonify({'success': False, 'error_code': 'ALREADY_PROCESSING', 'message': 'Un processo RSS è già attivo.'}), 409

    # --- Valida Input ---
    if not request.is_json: return jsonify(...), 400 # Errore content type
    data = request.get_json(); initial_feed_url = data.get('rss_url')
    if not initial_feed_url or not is_valid_url(initial_feed_url): return jsonify(...), 400 # Errore URL

    # --- Avvia Thread ---
    try:
        initial_status_for_thread = {
            'is_processing': True,
            'current_page': 0,
            'total_articles_processed': 0,
            'message': 'Avvio elaborazione feed in background...',
            'error': None
        }
        app_context = current_app.app_context()

        background_thread = threading.Thread(
            target=_background_rss_processing,
            args=(app_context, initial_feed_url, current_user_id, copy.deepcopy(initial_status_for_thread))
        )
        background_thread.daemon = True
        background_thread.start()
        logger.info(f"Thread background RSS avviato per: {initial_feed_url}")

        # Aggiorna stato globale DOPO avvio thread
        with rss_status_lock:
            rss_processing_status.update(initial_status_for_thread)

        return jsonify({
            'success': True,
            'message': 'Elaborazione feed avviata in background. Controlla lo stato periodicamente.'
        }), 202 # Accepted

    except Exception as e_start:
        logger.exception("Errore CRITICO avvio thread RSS.")
        with rss_status_lock: # Resetta stato globale se fallisce avvio
            rss_processing_status['is_processing'] = False
            rss_processing_status['message'] = f"Errore avvio processo: {e_start}"
            rss_processing_status['error'] = str(e_start)
        return jsonify({'success': False, 'error_code': 'THREAD_START_FAILED', 'message': f'Errore avvio processo background: {e_start}'}), 500

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

        sql_query = "SELECT article_id, title, article_url, extracted_content_path FROM articles WHERE processing_status = 'completed' AND extracted_content_path IS NOT NULL"
        params = []
        if app_mode == 'saas':
           sql_query += " AND user_id = ?"
           params.append(current_user_id)
        sql_query += " ORDER BY published_at DESC" # O added_at

        logger.info(f"Esecuzione query per contenuti articoli (Filtro Utente: {'Sì' if app_mode=='saas' else 'No'})...")
        cursor.execute(sql_query, tuple(params))

        for row in cursor.fetchall():
            article_id = row['article_id']
            title = row['title']
            article_url = row['article_url']
            content_path = row['extracted_content_path']
            content = None

            if not content_path:
                logger.warning(f"[{article_id}] Percorso file contenuto mancante nel DB per '{title}'.")
                articles_read_errors.append(f"{article_id}: Path Mancante")
                continue

            try:
                # Verifica esistenza file prima di aprirlo
                if not os.path.exists(content_path):
                    logger.error(f"[{article_id}] File contenuto NON TROVATO: {content_path} per '{title}'.")
                    articles_read_errors.append(f"{article_id}: File Non Trovato ({os.path.basename(content_path)})")
                    continue # Salta questo articolo

                with open(content_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                articles_processed_count += 1

                all_articles_content.write(f"--- ARTICOLO START ---\n")
                all_articles_content.write(f"ID: {article_id}\n")
                all_articles_content.write(f"Titolo: {title}\n")
                all_articles_content.write(f"URL: {article_url}\n")
                all_articles_content.write(f"--- Contenuto ---\n{content}\n")
                all_articles_content.write(f"--- ARTICOLO END ---\n\n\n")

            except (IOError, OSError) as e:
                logger.error(f"[{article_id}] Errore lettura file {content_path} per '{title}': {e}")
                articles_read_errors.append(f"{article_id}: Errore Lettura ({e})")
            except Exception as e_gen:
                 logger.error(f"[{article_id}] Errore generico lettura/scrittura buffer per '{title}': {e_gen}")
                 articles_read_errors.append(f"{article_id}: Errore Generico ({e_gen})")


        conn.close()
        logger.info(f"Recuperati contenuti da {articles_processed_count} articoli. Errori lettura file: {len(articles_read_errors)}.")
        if articles_read_errors:
             logger.warning(f"Dettaglio errori lettura file: {articles_read_errors}")

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

    # Aggiungi un header al file se ci sono stati errori di lettura
    if articles_read_errors:
         error_header = f"ATTENZIONE: Impossibile leggere il contenuto di {len(articles_read_errors)} articoli.\n"
         error_header += "Dettagli (ID Articolo: Motivo):\n"
         for err in articles_read_errors:
             error_header += f"- {err}\n"
         error_header += "---\n\n"
         file_content = error_header + file_content


    return Response(
        file_content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment;filename={output_filename}"}
    )


# --- NUOVA ROUTE: Elimina Tutti gli Articoli Utente ---
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
    files_to_delete = []
    sqlite_rows_affected = 0
    sqlite_rows_after_delete = -1
    chroma_deleted = False
    chroma_error = None
    files_deleted_count = 0
    file_delete_errors = []

    try:
        # --- 1. Recupera percorsi file PRIMA di eliminare da DB ---
        logger.info(f"[{current_user_id}] Recupero percorsi file articoli da eliminare...")
        conn_sqlite = sqlite3.connect(db_path)
        cursor_sqlite = conn_sqlite.cursor()
        cursor_sqlite.execute("SELECT article_id, extracted_content_path FROM articles WHERE user_id = ?", (current_user_id,))
        articles_data = cursor_sqlite.fetchall()
        files_to_delete = [row[1] for row in articles_data if row[1]] # Lista dei percorsi validi
        logger.info(f"[{current_user_id}] Trovati {len(files_to_delete)} file .txt associati agli articoli.")

        # --- 2. Elimina da SQLite ---
        logger.info(f"[{current_user_id}] ESECUZIONE DELETE FROM articles WHERE user_id = ?...")
        cursor_sqlite.execute("DELETE FROM articles WHERE user_id = ?", (current_user_id,))
        sqlite_rows_affected = cursor_sqlite.rowcount
        conn_sqlite.commit()
        logger.info(f"[{current_user_id}] COMMIT SQLite eseguito. Righe affette: {sqlite_rows_affected}.")
        # Verifica opzionale
        cursor_sqlite.execute("SELECT COUNT(*) FROM articles WHERE user_id = ?", (current_user_id,))
        sqlite_rows_after_delete = cursor_sqlite.fetchone()[0]
        if sqlite_rows_after_delete != 0: logger.error(f"!!! VERIFICA DELETE SQLITE FALLITA: {sqlite_rows_after_delete} righe rimaste!")

    except sqlite3.Error as e_sql:
        logger.error(f"[{current_user_id}] Errore SQLite durante eliminazione articoli: {e_sql}", exc_info=True)
        if conn_sqlite: conn_sqlite.rollback()
        # Non chiudere la connessione qui, lo fa il finally
        # Restituisci errore immediato perché l'operazione critica è fallita
        return jsonify({'success': False, 'error_code': 'DB_DELETE_FAILED', 'message': f'Errore DB durante eliminazione: {e_sql}'}), 500
    finally:
         # Chiudi connessione DB *dopo* aver tentato il delete/commit/rollback
         if conn_sqlite: conn_sqlite.close(); logger.debug(f"[{current_user_id}] Connessione SQLite chiusa per delete all.")

    # --- 3. Elimina Collezione ChromaDB ---
    logger.info(f"[{current_user_id}] Tentativo eliminazione collezione ChromaDB: '{user_article_collection_name}'...")
    try:
        # Verifica se esiste prima di tentare delete (opzionale ma evita eccezioni inutili)
        # collections = chroma_client.list_collections()
        # collection_names = [c.name for c in collections]
        # if user_article_collection_name in collection_names:
        chroma_client.delete_collection(name=user_article_collection_name)
        logger.info(f"[{current_user_id}] Comando delete_collection per '{user_article_collection_name}' inviato (potrebbe non esistere).")
        chroma_deleted = True # Segna come tentato/riuscito (delete non dà errore se non esiste)
        # else:
        #     logger.info(f"[{current_user_id}] Collezione Chroma '{user_article_collection_name}' non trovata, nessuna eliminazione necessaria.")
        #     chroma_deleted = True # Consideriamo successo perché non c'era
    except Exception as e_chroma:
        logger.error(f"[{current_user_id}] Errore durante eliminazione collezione ChromaDB '{user_article_collection_name}': {e_chroma}", exc_info=True)
        chroma_deleted = False
        chroma_error = str(e_chroma)

    # --- 4. Elimina File Fisici ---
    logger.info(f"[{current_user_id}] Tentativo eliminazione di {len(files_to_delete)} file .txt...")
    for file_path in files_to_delete:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                files_deleted_count += 1
            elif file_path:
                 logger.warning(f"[{current_user_id}] File non trovato durante eliminazione: {file_path}")
                 file_delete_errors.append(f"{os.path.basename(file_path)}: Not Found")
            # else: Non dovrebbe accadere se abbiamo filtrato prima
        except OSError as e_os:
            logger.error(f"[{current_user_id}] Errore eliminazione file {file_path}: {e_os}")
            file_delete_errors.append(f"{os.path.basename(file_path)}: {e_os.strerror}")
        except Exception as e_file_gen:
             logger.error(f"[{current_user_id}] Errore generico eliminazione file {file_path}: {e_file_gen}")
             file_delete_errors.append(f"{os.path.basename(file_path)}: Unexpected Error")
    logger.info(f"[{current_user_id}] Eliminazione file completata. File eliminati: {files_deleted_count}/{len(files_to_delete)}. Errori: {len(file_delete_errors)}.")

    # --- 5. Risposta Finale ---
    final_success = (sqlite_rows_after_delete == 0 and not file_delete_errors) # Successo se DB pulito e nessun errore file
    message = (f"Eliminazione articoli utente {current_user_id}: "
               f"SQLite({sqlite_rows_affected} righe affette inizialmente, {sqlite_rows_after_delete} rimaste dopo verifica). "
               f"Chroma({('Tentata' if chroma_deleted else 'Fallita')}). "
               f"File({files_deleted_count}/{len(files_to_delete)} eliminati).")
    if file_delete_errors: message += f" Errori file: {len(file_delete_errors)}."
    if chroma_error: message += f" Errore Chroma: {chroma_error}."

    return jsonify({
        'success': final_success,
        'message': message,
        'details': {
            'sqlite_rows_affected': sqlite_rows_affected,
            'sqlite_rows_verified': sqlite_rows_after_delete,
            'chroma_deleted_attempted': chroma_deleted,
            'chroma_error': chroma_error,
            'files_found': len(files_to_delete),
            'files_deleted': files_deleted_count,
            'file_errors': file_delete_errors
        }
    }), 200 if final_success else 500 # 200 se tutto OK, 500 se DB o file hanno avuto problemi
