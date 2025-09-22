import logging
import os
import sqlite3
import threading
import shutil
import copy
from flask import Blueprint, current_app, send_from_directory, flash, redirect, url_for, request, jsonify
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

# Importiamo le funzioni di re-indicizzazione che abbiamo creato/identificato
from app.api.routes.videos import _reindex_video_from_db
from app.api.routes.documents import _index_document
from app.api.routes.rss import _index_article

logger = logging.getLogger(__name__)
protection_bp = Blueprint('protection', __name__)

# --- STATO GLOBALE e LOCK per il processo di Re-indicizzazione ---
reindex_status = {
    'is_processing': False,
    'total_items': 0,
    'processed_items': 0,
    'message': '',
    'error': None
}
reindex_status_lock = threading.Lock()


def _background_reindex_all_content(app_context, user_id: str):
    """
    Task in background per re-indicizzare tutti i contenuti di un utente.
    """
    global reindex_status, reindex_status_lock
    logger.info(f"[REINDEX_ALL] Avvio task in background per utente {user_id}")
    
    with app_context:
        db_path = current_app.config.get('DATABASE_FILE')
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=15.0)
            cursor = conn.cursor()

            # --- INIZIO MODIFICA MAGGIORE QUI ---

            # PRIMA: Aggiorna lo stato di tutti i contenuti dell'utente a 'failed_reindex'
            # per segnalare che devono essere tutti riprocessati.
            # Questo assicura che il sistema li veda come "non indicizzati" dopo il ripristino.
            logger.info(f"[REINDEX_ALL] Aggiorno lo stato di tutti i contenuti a 'failed_reindex' per utente {user_id}...")
            
            cursor.execute("UPDATE videos SET processing_status = 'failed_reindex' WHERE user_id = ?", (user_id,))
            cursor.execute("UPDATE documents SET processing_status = 'failed_reindex' WHERE user_id = ?", (user_id,))
            cursor.execute("UPDATE articles SET processing_status = 'failed_reindex' WHERE user_id = ?", (user_id,))
            cursor.execute("UPDATE pages SET processing_status = 'failed_reindex' WHERE user_id = ?", (user_id,))
            conn.commit() # Salviamo subito questi cambiamenti
            logger.info(f"[REINDEX_ALL] Stati dei contenuti aggiornati. Inizio la re-indicizzazione.")

            # 1. Ottieni la lista di TUTTI gli elementi da re-indicizzare
            # Adesso selezioniamo tutti i video, documenti e articoli per l'utente,
            # dato che il loro stato è stato appena impostato a 'failed_reindex'.
            cursor.execute("SELECT video_id FROM videos WHERE user_id = ?", (user_id,))
            videos_to_index = [row[0] for row in cursor.fetchall()]
            
            cursor.execute("SELECT doc_id FROM documents WHERE user_id = ?", (user_id,))
            docs_to_index = [row[0] for row in cursor.fetchall()]
            
            cursor.execute("SELECT article_id FROM articles WHERE user_id = ?", (user_id,))
            articles_to_index = [row[0] for row in cursor.fetchall()]

            cursor.execute("SELECT page_id FROM pages WHERE user_id = ?", (user_id,)) # Aggiungiamo anche le pagine
            pages_to_index = [row[0] for row in cursor.fetchall()]

            total_items = len(videos_to_index) + len(docs_to_index) + len(articles_to_index) + len(pages_to_index)
            processed_count = 0

            with reindex_status_lock:
                reindex_status['total_items'] = total_items
                reindex_status['processed_items'] = 0

            # 2. Processa ogni categoria
            for item_id in videos_to_index:
                with reindex_status_lock:
                    processed_count += 1
                    reindex_status['processed_items'] = processed_count
                    reindex_status['message'] = f"Re-indicizzazione video ({processed_count}/{total_items})..."
                _reindex_video_from_db(item_id, conn, user_id)
                conn.commit()

            for item_id in docs_to_index:
                with reindex_status_lock:
                    processed_count += 1
                    reindex_status['processed_items'] = processed_count
                    reindex_status['message'] = f"Re-indicizzazione documenti ({processed_count}/{total_items})..."
                _index_document(item_id, conn, user_id)
                conn.commit()

            for item_id in articles_to_index:
                with reindex_status_lock:
                    processed_count += 1
                    reindex_status['processed_items'] = processed_count
                    reindex_status['message'] = f"Re-indicizzazione articoli ({processed_count}/{total_items})..."
                _index_article(item_id, conn, user_id)
                conn.commit()

            for item_id in pages_to_index: # Aggiungiamo il loop per le pagine
                with reindex_status_lock:
                    processed_count += 1
                    reindex_status['processed_items'] = processed_count
                    reindex_status['message'] = f"Re-indicizzazione pagine ({processed_count}/{total_items})..."
                # Assumiamo che esista una funzione _index_page come _index_article
                from app.api.routes.website import _index_page # Importa qui o all'inizio del file
                _index_page(item_id, conn, user_id)
                conn.commit()
            
            # --- FINE MODIFICA MAGGIORE ---

            with reindex_status_lock:
                reindex_status['message'] = "Re-indicizzazione completata con successo!"
        
        except Exception as e:
            logger.error(f"[REINDEX_ALL] Errore critico nel task in background: {e}", exc_info=True)
            with reindex_status_lock:
                reindex_status['error'] = f"Errore: {e}"
        finally:
            if conn:
                conn.close()
            with reindex_status_lock:
                reindex_status['is_processing'] = False
            logger.info(f"[REINDEX_ALL] Task in background per utente {user_id} terminato.")

@protection_bp.route('/download/database')
@login_required
def download_database_backup():
    # ... (questa funzione rimane identica a prima) ...
    try:
        db_path = current_app.config.get('DATABASE_FILE')
        if not db_path:
            raise ValueError("Percorso del database non configurato.")

        db_directory = os.path.dirname(db_path)
        db_filename = os.path.basename(db_path)
        
        safe_user_id = "".join(c for c in current_user.id if c.isalnum() or c in ('-', '_')).rstrip()
        download_filename = f"magazzino_backup_{safe_user_id}_{db_filename}"

        logger.info(f"Utente {current_user.id} ha richiesto il download del backup del database. Nome file: {download_filename}")

        return send_from_directory(
            directory=db_directory,
            path=db_filename,
            as_attachment=True,
            download_name=download_filename
        )

    except Exception as e:
        logger.error(f"Errore durante la creazione del backup del database per l'utente {current_user.id}: {e}", exc_info=True)
        flash('Si è verificato un errore imprevisto durante la preparazione del download del database.', 'error')
        return redirect(url_for('settings.settings_page', _anchor='protezione'))


@protection_bp.route('/restore/database', methods=['POST'])
@login_required
def restore_database_backup():
    """
    Riceve un file .db, sostituisce quello corrente e avvia la re-indicizzazione.
    """
    global reindex_status, reindex_status_lock

    with reindex_status_lock:
        if reindex_status['is_processing']:
            return jsonify({'success': False, 'message': 'Un processo di re-indicizzazione è già in corso.'}), 409

    if 'backup_file' not in request.files:
        return jsonify({'success': False, 'message': 'Nessun file di backup fornito.'}), 400

    file = request.files['backup_file']
    if file.filename == '' or not file.filename.endswith('.db'):
        return jsonify({'success': False, 'message': 'File non valido. Seleziona un file .db.'}), 400

    try:
        db_path = current_app.config.get('DATABASE_FILE')
        
        # 1. Salva il file caricato temporaneamente
        temp_filename = os.path.join(os.path.dirname(db_path), secure_filename(f"temp_restore_{file.filename}"))
        file.save(temp_filename)
        
        # 2. Sostituisci il database corrente con il backup
        #    (shutil.move è più sicuro di os.rename attraverso diversi filesystem)
        shutil.move(temp_filename, db_path)
        logger.info(f"Database ripristinato con successo da backup per l'utente {current_user.id}")

        # --- INIZIO MODIFICA: ELIMINAZIONE CARTELLA CHROMADB ---
        chroma_persist_path = current_app.config.get('CHROMA_PERSIST_PATH')
        if chroma_persist_path and os.path.exists(chroma_persist_path):
            try:
                # Importiamo shutil qui per essere sicuri che sia disponibile
                shutil.rmtree(chroma_persist_path)
                logger.info(f"Directory ChromaDB eliminata con successo: {chroma_persist_path}")
            except OSError as e:
                logger.error(f"Errore eliminando la directory ChromaDB {chroma_persist_path}: {e}")
                # Non blocchiamo il ripristino per questo, ma segnaliamo l'errore
        else:
            logger.warning(f"Percorso ChromaDB '{chroma_persist_path}' non trovato o non esistente. Nessuna directory da eliminare.")
        # --- FINE MODIFICA ---

        # 3. Avvia il task di re-indicizzazione in background
        with reindex_status_lock:
            reindex_status.update({
                'is_processing': True,
                'total_items': 0,
                'processed_items': 0,
                'message': 'Avvio re-indicizzazione...',
                'error': None
            })
        
        app_context = current_app.app_context()
        background_thread = threading.Thread(
            target=_background_reindex_all_content,
            args=(app_context, current_user.id)
        )
        background_thread.daemon = True
        background_thread.start()

        return jsonify({'success': True, 'message': 'Ripristino avviato. Inizio re-indicizzazione in background.'}), 202

    except Exception as e:
        logger.error(f"Errore critico durante il ripristino del database: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Errore: {e}'}), 500



@protection_bp.route('/reindex-progress')
@login_required
def get_reindex_progress():
    """
    Endpoint di polling per ottenere lo stato del processo di re-indicizzazione.
    """
    global reindex_status, reindex_status_lock
    with reindex_status_lock:
        return jsonify(copy.deepcopy(reindex_status))