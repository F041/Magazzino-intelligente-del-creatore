# FILE: app/api/routes/keys.py

import logging
import sqlite3
from flask import Blueprint, request, jsonify, current_app, render_template, flash, redirect, url_for
from flask_login import login_required, current_user

# Importa la funzione helper per generare chiavi
from app.utils import generate_api_key
import datetime
import jwt

try:
    from app.api.routes.search import require_api_key
except ImportError:
    # Fallback se l'import diretto fallisce (improbabile ma sicuro)
    logger.error("Impossibile importare il decoratore require_api_key da search.py!")
    # Definisci un decoratore fittizio per evitare crash all'avvio
    # MA QUESTO NON FORNIRÀ SICUREZZA! DA RISOLVERE SE ACCADE.
    def require_api_key(f):
        return f

logger = logging.getLogger(__name__)
keys_bp = Blueprint('keys', __name__) # Crea il Blueprint

# --- Route per Mostrare la Pagina di Gestione ---
@keys_bp.route('/manage') # Il prefisso /api-keys verrà aggiunto in main.py
@login_required
def manage_api_keys_page():
    """Mostra la pagina di gestione delle chiavi API per l'utente loggato."""
    user_keys = []
    db_path = current_app.config.get('DATABASE_FILE')
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, key, name, created_at, last_used_at, is_active FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (current_user.id,))
        user_keys = cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Errore DB leggendo API keys per utente {current_user.id}: {e}")
        flash('Errore nel caricamento delle chiavi API.', 'error')
    finally:
        if conn: conn.close()

    # Passiamo anche config per poter controllare APP_MODE nel template se serve
    return render_template('api_keys.html', api_keys=user_keys, config=current_app.config)

# --- Route per Generare una Nuova Chiave (POST dal form) ---
@keys_bp.route('/generate', methods=['POST'])
@login_required
def generate_api_key_action():
    """Genera una nuova chiave API per l'utente loggato e la salva nel DB."""
    key_name = request.form.get('key_name')
    new_key_value = generate_api_key() # Genera chiave sicura

    db_path = current_app.config.get('DATABASE_FILE')
    conn = None
    success = False
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO api_keys (user_id, key, name) VALUES (?, ?, ?)",
            (current_user.id, new_key_value, key_name if key_name else None)
        )
        conn.commit()
        logger.info(f"Nuova chiave API generata per utente {current_user.id}. Nome: {key_name if key_name else 'N/A'}")
        flash(f"Nuova chiave API generata con successo! Copiala ora, non sarà mostrata di nuovo: {new_key_value}", 'success')
        success = True
    except sqlite3.Error as e:
        logger.error(f"Errore DB generando API key per utente {current_user.id}: {e}")
        if conn: conn.rollback()
        flash('Errore durante la generazione della chiave API nel database.', 'error')
    finally:
        if conn: conn.close()

    # Reindirizza sempre alla pagina di gestione
    return redirect(url_for('keys.manage_api_keys_page')) # Usa il nome del blueprint 'keys.'

# --- Route API per Eliminare una Chiave Specifica (usata dal JS) ---
@keys_bp.route('/api/<int:key_id>', methods=['DELETE']) 
@login_required
def delete_api_key_action(key_id):
     """Elimina una specifica chiave API appartenente all'utente loggato."""
     db_path = current_app.config.get('DATABASE_FILE')
     conn = None
     try:
         conn = sqlite3.connect(db_path)
         cursor = conn.cursor()
         cursor.execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, current_user.id))
         if cursor.rowcount > 0:
             conn.commit()
             logger.info(f"Chiave API ID {key_id} eliminata per utente {current_user.id}.")
             return jsonify({'success': True, 'message': 'Chiave API eliminata con successo.'})
         else:
             conn.rollback()
             logger.warning(f"Tentativo eliminazione chiave API ID {key_id} fallito (non trovata o non appartenente all'utente {current_user.id}).")
             return jsonify({'success': False, 'error_code': 'NOT_FOUND_OR_FORBIDDEN', 'message': 'Chiave API non trovata o non autorizzato.'}), 404
     except sqlite3.Error as e:
         logger.error(f"Errore DB eliminando chiave API ID {key_id} per utente {current_user.id}: {e}")
         if conn: conn.rollback()
         return jsonify({'success': False, 'error_code': 'DB_ERROR', 'message': 'Errore database durante eliminazione chiave.'}), 500
     finally:
         if conn: conn.close()


@keys_bp.route('/api/verify', methods=['GET']) 
@require_api_key # stesso decoratore usato per la ricerca
def verify_api_key_endpoint(*args, **kwargs):
    """
    Endpoint usato da Streamlit per verificare una chiave API.
    Il decoratore @require_api_key fa tutto il lavoro di validazione.
    Se il decoratore passa, la chiave è valida.
    """
    # Se siamo arrivati qui, il decoratore @require_api_key ha validato
    # con successo la chiave API presente nell'header X-API-Key.
    # Possiamo semplicemente restituire un successo.

    # Potremmo opzionalmente recuperare l'user_id passato dal decoratore
    # per loggare chi ha verificato, ma non è strettamente necessario.
    user_id_verified = kwargs.get('api_user_id_override') # Recupera ID se passato
    logger.info(f"Verifica API Key riuscita per utente ID: {user_id_verified if user_id_verified else 'Sessione Utente (non API Key)'}")

    return jsonify({'success': True, 'message': 'Chiave API valida.'})

# --- (Opzionale) Route API per Attivare/Disattivare Chiave ---
# @keys_bp.route('/<int:key_id>/toggle', methods=['POST'])
# @login_required
# def toggle_api_key_action(key_id):
#     # Implementare logica per cambiare il flag 'is_active' nel DB
#     pass

# --- NUOVA ROUTE API PER GENERARE TOKEN JWT PER LA CHAT ESTERNA ---
@keys_bp.route('/api/generate-token', methods=['POST'])
@login_required
def generate_jwt_for_widget():
    """
    Genera un token JWT per un utente esterno (es. dipendente).
    Il token è firmato con la SECRET_KEY dell'app e contiene l'user_id dell'admin che lo ha generato.
    """
    if not request.is_json:
        return jsonify({'success': False, 'error_code': 'INVALID_CONTENT_TYPE', 'message': 'Request must be JSON.'}), 400

    data = request.get_json()
    user_identifier = data.get('user_id')
    try:
        # L'input è in secondi (es. '86400'), lo convertiamo in intero
        expires_in_seconds = int(data.get('expires_in', 86400))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': 'Durata di validità non valida.'}), 400
    
    if not user_identifier:
        return jsonify({'success': False, 'error_code': 'VALIDATION_ERROR', 'message': 'Identificativo utente richiesto.'}), 400

    # L'utente a cui associare i dati è l'admin che sta generando il token
    admin_user_id = current_user.id
    secret_key = current_app.config.get('SECRET_KEY')

    if not secret_key:
        logger.error(f"Tentativo di generare JWT senza una SECRET_KEY configurata!")
        return jsonify({'success': False, 'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Errore di configurazione del server.'}), 500

    # Creiamo il payload del token
    payload = {
        'iat': datetime.datetime.utcnow(), # Data di creazione (Issued At)
        'exp': datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in_seconds), # Data di scadenza (Expiration)
        'sub': admin_user_id, # Il "soggetto" del token è l'utente admin proprietario dei dati
        'name': user_identifier # Il nome descrittivo del dipendente
    }

    try:
        # Codifichiamo il token usando la chiave segreta dell'applicazione
        encoded_jwt = jwt.encode(payload, secret_key, algorithm="HS256")
        logger.info(f"Admin {admin_user_id} ha generato un token JWT per '{user_identifier}' valido per {expires_in_seconds} secondi.")
        
        # Restituiamo il token al frontend
        return jsonify({'success': True, 'token': encoded_jwt})

    except Exception as e:
        logger.error(f"Errore durante la codifica del JWT: {e}", exc_info=True)
        return jsonify({'success': False, 'error_code': 'JWT_ENCODING_ERROR', 'message': 'Errore durante la creazione del token.'}), 500
    
    # --- NUOVA LOGICA PER IL WIDGET JWT ---

@keys_bp.route('/api/widget-settings', methods=['GET', 'POST']) # Aggiunto GET
@login_required
def widget_settings():
    """
    GET: Recupera il dominio autorizzato per il widget dell'utente.
    POST: Salva o aggiorna il dominio autorizzato per il widget di un utente.
    """
    db_path = current_app.config.get('DATABASE_FILE')
    conn = None

    if request.method == 'POST':
        if not request.is_json or 'domain' not in request.get_json():
            return jsonify({'success': False, 'message': 'Dati non validi.'}), 400
        
        domain = request.get_json().get('domain').strip().lower()
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET allowed_widget_domain = ? WHERE id = ?", (domain, current_user.id))
            conn.commit()
            logger.info(f"Utente {current_user.id} ha aggiornato il dominio del widget a: {domain}")
            return jsonify({'success': True, 'message': 'Dominio salvato!', 'customerId': current_user.id})
        except sqlite3.Error as e:
            logger.error(f"Errore DB durante l'aggiornamento del dominio per l'utente {current_user.id}: {e}")
            if conn: conn.rollback()
            return jsonify({'success': False, 'message': 'Errore durante il salvataggio.'}), 500
        finally:
            if conn: conn.close()

    if request.method == 'GET':
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT allowed_widget_domain FROM users WHERE id = ?", (current_user.id,))
            result = cursor.fetchone()
            domain = result[0] if result else ""
            return jsonify({'success': True, 'domain': domain})
        except sqlite3.Error as e:
            logger.error(f"Errore DB durante la lettura del dominio per l'utente {current_user.id}: {e}")
            return jsonify({'success': False, 'message': 'Errore nel recupero del dominio.'}), 500
        finally:
            if conn: conn.close()


@keys_bp.route('/api/public/generate-widget-token', methods=['POST'])
def generate_public_widget_token():
    """
    Endpoint PUBBLICO chiamato da embed.js.
    Verifica il customer_id e il dominio di origine prima di rilasciare un token JWT.
    """
    if not request.is_json:
        return jsonify({'success': False, 'message': 'Richiesta non valida.'}), 400
        
    customer_id = request.json.get('customerId')
    origin_domain = request.headers.get('Origin') # Es. https://www.sito-del-cliente.com

    if not customer_id or not origin_domain:
        return jsonify({'success': False, 'message': 'Dati mancanti.'}), 400
    
    # Pulisce il dominio di origine, rimuovendo protocollo, path E PORTA.
    cleaned_origin = origin_domain.replace('https://', '').replace('http://', '').split('/')[0].split(':')[0]

    db_path = current_app.config.get('DATABASE_FILE')
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT allowed_widget_domain FROM users WHERE id = ?", (customer_id,))
        result = cursor.fetchone()
        
        if not result or not result[0]:
            logger.warning(f"Tentativo di generazione token per customer ID non valido o senza dominio configurato: {customer_id}")
            return jsonify({'success': False, 'message': 'Cliente non autorizzato.'}), 403

        allowed_domain = result[0].lower()
        
        # --- IL CONTROLLO DI SICUREZZA FONDAMENTALE ---
        if cleaned_origin != allowed_domain.replace('www.', ''):
            logger.warning(f"Tentativo di generazione token RIFIUTATO. Dominio di origine '{cleaned_origin}' non corrisponde a quello autorizzato '{allowed_domain}' per il cliente {customer_id}.")
            return jsonify({'success': False, 'message': 'Dominio non autorizzato.'}), 403

        # Se i controlli passano, genera il token JWT
        secret_key = current_app.config.get('SECRET_KEY')
        payload = {
            'iat': datetime.datetime.utcnow(),
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=2), # Token valido per 2 ore
            'sub': customer_id, # Il "soggetto" è l'ID del cliente proprietario dei dati
            'aud': 'widget_user' # Audience: identifica che è un token per un utente del widget
        }
        encoded_jwt = jwt.encode(payload, secret_key, algorithm="HS256")
        
        return jsonify({'success': True, 'token': encoded_jwt})

    except Exception as e:
        logger.error(f"Errore imprevisto durante la generazione del token pubblico: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Errore interno del server.'}), 500
    finally:
        if conn: conn.close()