# FILE: app/api/routes/keys.py

import logging
import sqlite3
from flask import Blueprint, request, jsonify, current_app, render_template, flash, redirect, url_for
from flask_login import login_required, current_user

# Importa la funzione helper per generare chiavi
from app.utils import generate_api_key

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