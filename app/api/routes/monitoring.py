# FILE: app/api/routes/monitoring.py
import logging
import sqlite3
from flask import Blueprint, request, jsonify, current_app 
from flask_login import login_required, current_user
from app.services.youtube.client import YouTubeClient

# Importa funzioni YouTube/RSS se necessario per validare/recuperare nomi/ID
# (Opzionale per ora, possiamo aggiungerlo dopo)
# from app.services.youtube.client import YouTubeClient
# import feedparser

logger = logging.getLogger(__name__)
monitoring_bp = Blueprint('monitoring', __name__)

# === API PER OTTENERE LO STATO ATTUALE ===
@monitoring_bp.route('/status', methods=['GET'])
@login_required
def get_monitoring_status():
    """Restituisce le sorgenti (max 1 per tipo) attualmente monitorate dall'utente."""
    user_id = current_user.id
    db_path = current_app.config.get('DATABASE_FILE')
    monitored_channel = None
    monitored_feed = None
    conn = None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Cerca il canale YouTube attivo (assumiamo ce ne sia max 1 attivo per utente per ora)
        cursor.execute("""
            SELECT id, channel_id, channel_url, channel_name, last_checked_at, is_active
            FROM monitored_youtube_channels
            WHERE user_id = ? AND is_active = TRUE
            LIMIT 1
        """, (user_id,))
        channel_row = cursor.fetchone()
        if channel_row:
            monitored_channel = dict(channel_row)

        # Cerca il feed RSS attivo
        cursor.execute("""
            SELECT id, feed_url, feed_title, last_checked_at, is_active
            FROM monitored_rss_feeds
            WHERE user_id = ? AND is_active = TRUE
            LIMIT 1
        """, (user_id,))
        feed_row = cursor.fetchone()
        if feed_row:
            monitored_feed = dict(feed_row)

        conn.close()
        return jsonify({
            'success': True,
            'youtube_channel': monitored_channel,
            'rss_feed': monitored_feed
        }), 200

    except sqlite3.Error as e:
        logger.error(f"Errore DB leggendo stato monitoraggio per user {user_id}: {e}")
        if conn: conn.close()
        return jsonify({'success': False, 'error_code': 'DB_ERROR', 'message': 'Errore recupero stato monitoraggio.'}), 500
    except Exception as e_gen:
        logger.error(f"Errore generico get_monitoring_status: {e_gen}", exc_info=True)
        if conn: conn.close()
        return jsonify({'success': False, 'error_code': 'UNEXPECTED_ERROR', 'message': 'Errore server imprevisto.'}), 500


# === API PER AGGIUNGERE/SOSTITUIRE UNA SORGENTE MONITORATA ===
@monitoring_bp.route('/source', methods=['POST'])
@login_required
def add_or_replace_monitored_source():
    """Aggiunge o sostituisce la sorgente monitorata attiva per un dato tipo.
       Estrae l'ID corretto per i canali YouTube.
    """
    user_id = current_user.id
    db_path = current_app.config.get('DATABASE_FILE')
    token_path = current_app.config.get('TOKEN_PATH') # Necessario per YouTubeClient

    if not request.is_json:
        return jsonify({'success': False, 'error_code': 'INVALID_CONTENT_TYPE', 'message': 'Request must be JSON.'}), 400

    data = request.get_json()
    source_type = data.get('type')
    source_url = data.get('url') # Questo è l'URL originale fornito

    if source_type not in ['youtube', 'rss'] or not source_url:
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': 'Campi "type" (youtube/rss) e "url" richiesti.'}), 400

    # --- Logica specifica per tipo ---
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        if source_type == 'youtube':
            logger.info(f"Tentativo aggiunta/sostituzione monitoraggio YouTube per URL: {source_url}")
            # --- ESTRAI L'ID CANALE REALE ---
            if not token_path: # Verifica necessaria per YouTubeClient
                 logger.error("Token path non configurato, impossibile usare YouTubeClient.")
                 raise RuntimeError("Configurazione Token YouTube mancante.")
            try:
                yt_client = YouTubeClient(token_file=token_path)
                channel_id = yt_client.extract_channel_info(source_url) # Estrae l'ID UC...
                if not channel_id:
                    raise ValueError("Impossibile estrarre l'ID canale dall'URL fornito.")
                logger.info(f"ID Canale estratto: {channel_id}")
                # === RECUPERA NOME CANALE ===
                channel_name = None # Default
                try:
                    # Usa l'ID estratto per chiedere lo snippet
                    request = yt_client.youtube.channels().list(
                        part="snippet",
                        id=channel_id
                    )
                    response = request.execute()
                    if response and response.get('items'):
                        channel_name = response['items'][0]['snippet']['title']
                        logger.info(f"Nome canale recuperato: {channel_name}")
                    else:
                        logger.warning(f"Impossibile recuperare il nome per il canale ID: {channel_id}")
                except Exception as e_name:
                    logger.warning(f"Errore durante recupero nome canale {channel_id}: {e_name}")
            except Exception as e_extract:
                logger.error(f"Errore estrazione ID canale da URL '{source_url}': {e_extract}")
                # Restituisci errore al client perché l'URL non è valido per YT o c'è un problema API
                if conn: conn.close() # Chiudi connessione prima di uscire
                return jsonify({'success': False, 'error_code': 'INVALID_YOUTUBE_URL_OR_API_ERROR', 'message': f"Impossibile processare l'URL YouTube: {e_extract}"}), 400
            # --- FINE ESTRAZIONE ID ---

            # Ora usa channel_id (UC...) per il DB e source_url per riferimento
            logger.info(f"Disattivazione vecchi canali YouTube monitorati per user {user_id}")
            cursor.execute("UPDATE monitored_youtube_channels SET is_active = FALSE WHERE user_id = ?", (user_id,))

            cursor.execute("UPDATE ...")

            # Salva ID estratto, URL originale E NOME RECUPERATO
            cursor.execute("""
                INSERT INTO monitored_youtube_channels (user_id, channel_id, channel_url, channel_name, is_active)
                VALUES (?, ?, ?, ?, TRUE)
                ON CONFLICT(user_id, channel_id) DO UPDATE SET
                    channel_url=excluded.channel_url,
                    channel_name=excluded.channel_name, /* <-- Aggiorna anche il nome */
                    is_active=TRUE,
                    last_checked_at=NULL
            """, (user_id, channel_id, source_url, channel_name)) 
            message = f"Canale YouTube '{channel_name or channel_id}' impostato per monitoraggio." 

        elif source_type == 'rss':
            # La logica RSS rimane uguale, usa direttamente source_url
            logger.info(f"Tentativo aggiunta/sostituzione monitoraggio RSS per URL: {source_url}")
            logger.info(f"Disattivazione vecchi feed RSS monitorati per user {user_id}")
            cursor.execute("UPDATE monitored_rss_feeds SET is_active = FALSE WHERE user_id = ?", (user_id,))
            # TODO (Opzionale): Estrarre feed_title da source_url
            feed_title = None
            cursor.execute("""
                INSERT INTO monitored_rss_feeds (user_id, feed_url, feed_title, is_active)
                VALUES (?, ?, ?, TRUE)
                ON CONFLICT(user_id, feed_url) DO UPDATE SET
                    feed_title=excluded.feed_title,
                    is_active=TRUE,
                    last_checked_at=NULL
            """, (user_id, source_url, feed_title))
            message = f"Feed RSS '{source_url}' impostato per monitoraggio."

        conn.commit()
        conn.close()
        logger.info(f"User {user_id} ha aggiornato sorgente {source_type}. Messaggio: {message}")
        return jsonify({'success': True, 'message': message}), 201

    # ... (Gestione eccezioni DB e generiche come prima) ...
    except RuntimeError as rte: # Errore config token
         logger.error(f"Errore Runtime (config?) per user {user_id}: {rte}")
         if conn: conn.rollback(); conn.close()
         return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': str(rte)}), 500
    except sqlite3.Error as e_sql:
        logger.error(f"Errore DB aggiungendo sorgente {source_type} per user {user_id}: {e_sql}")
        if conn: conn.rollback(); conn.close()
        return jsonify({'success': False, 'error_code': 'DB_ERROR', 'message': f'Errore database: {e_sql}'}), 500
    except Exception as e_gen:
        logger.error(f"Errore generico add_or_replace_monitored_source: {e_gen}", exc_info=True)
        if conn: conn.rollback(); conn.close()
        return jsonify({'success': False, 'error_code': 'UNEXPECTED_ERROR', 'message': 'Errore server imprevisto.'}), 500

# === API PER RIMUOVERE/DISATTIVARE UNA SORGENTE MONITORATA ===
@monitoring_bp.route('/source', methods=['DELETE'])
@login_required
def remove_monitored_source():
    """Disattiva il monitoraggio per un dato tipo di sorgente."""
    user_id = current_user.id
    db_path = current_app.config.get('DATABASE_FILE')

    if not request.is_json:
        return jsonify({'success': False, 'error_code': 'INVALID_CONTENT_TYPE', 'message': 'Request must be JSON.'}), 400

    data = request.get_json()
    source_type = data.get('type')

    if source_type not in ['youtube', 'rss']:
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': 'Campo "type" (youtube/rss) richiesto.'}), 400

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        rows_affected = 0

        if source_type == 'youtube':
            logger.info(f"Disattivazione monitoraggio canale YouTube per user {user_id}")
            cursor.execute("UPDATE monitored_youtube_channels SET is_active = FALSE WHERE user_id = ?", (user_id,))
            rows_affected = cursor.rowcount
            message = "Monitoraggio canale YouTube disattivato."
        elif source_type == 'rss':
            logger.info(f"Disattivazione monitoraggio feed RSS per user {user_id}")
            cursor.execute("UPDATE monitored_rss_feeds SET is_active = FALSE WHERE user_id = ?", (user_id,))
            rows_affected = cursor.rowcount
            message = "Monitoraggio feed RSS disattivato."

        conn.commit()
        conn.close()
        logger.info(f"User {user_id} ha disattivato sorgente {source_type}. Righe modificate: {rows_affected}")
        # Restituisce successo anche se non c'era nulla da disattivare (rows_affected=0)
        return jsonify({'success': True, 'message': message}), 200

    except sqlite3.Error as e_sql:
        logger.error(f"Errore DB disattivando sorgente {source_type} per user {user_id}: {e_sql}")
        if conn: conn.rollback(); conn.close()
        return jsonify({'success': False, 'error_code': 'DB_ERROR', 'message': f'Errore database: {e_sql}'}), 500
    except Exception as e_gen:
        logger.error(f"Errore generico remove_monitored_source: {e_gen}", exc_info=True)
        if conn: conn.rollback(); conn.close()
        return jsonify({'success': False, 'error_code': 'UNEXPECTED_ERROR', 'message': 'Errore server imprevisto.'}), 500