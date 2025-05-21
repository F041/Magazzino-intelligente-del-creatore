import pytest
import sqlite3
from flask import url_for, session
from app.models.user import User # Utile per creare un utente di test
import logging

logger = logging.getLogger(__name__)

# Helper per registrare e loggare un utente e restituire l'ID utente e la sessione client
# Potrebbe essere spostato in conftest.py se usato da più moduli di test API
def register_and_login_for_api_keys(client, app, email="keyuser@example.com", password="password"):
    logger.info(f"HELPER: Tentativo registrazione per: {email}")
    response_register = client.post(url_for('register'), data={
        'email': email, 'password': password, 'confirm_password': password
    }, follow_redirects=True)

    # Controlla se la registrazione ha mostrato un messaggio di email già esistente
    # Questo potrebbe succedere se il DB non viene pulito perfettamente tra i test se eseguiti più volte.
    # Tuttavia, con scope='session' per app, il DB dovrebbe essere pulito solo una volta all'inizio.
    if b"Email gi\xc3\xa0 registrata" in response_register.data:
        logger.warning(f"HELPER: Registrazione per {email} ha indicato email già esistente. Procedo con login.")
    else:
        logger.info(f"HELPER: Registrazione per {email} sembra essere andata a buon fine (o nessun messaggio di errore specifico).")

    logger.info(f"HELPER: Tentativo login per: {email}")
    client.post(url_for('login'), data={'email': email, 'password': password}, follow_redirects=True)

    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        logger.info(f"HELPER: Eseguo SELECT id FROM users WHERE email = '{email}'")
        cursor.execute("SELECT id, email FROM users WHERE email = ?", (email,)) # Seleziona anche email per conferma
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
            logger.info(f"HELPER: Utente trovato nel DB: ID={user_id}, Email={user_row[1]}")
        else:
            logger.warning(f"HELPER: Utente {email} NON trovato nel DB dopo tentativi di register/login.")
            # Stampa tutti gli utenti per vedere cosa c'è
            cursor.execute("SELECT id, email FROM users")
            all_users = cursor.fetchall()
            logger.warning(f"HELPER: Utenti attualmente nel DB: {all_users}")
        conn.close()
    assert user_id is not None, f"User ID non trovato per {email} dopo login nel test helper"
    return user_id

# --- Test per la Pagina di Gestione Chiavi ---
def test_manage_api_keys_page_loads_unauthenticated(client):
    """Testa che un utente non loggato venga reindirizzato al login."""
    response = client.get(url_for('keys.manage_api_keys_page'), follow_redirects=True)
    assert response.status_code == 200
    assert b"Login" in response.data # Reindirizzato alla pagina di login
    assert b"Per favore, effettua il login" in response.data

def test_manage_api_keys_page_loads_authenticated_no_keys(client, app):
    """Testa la pagina di gestione per un utente loggato senza chiavi API."""
    register_and_login_for_api_keys(client, app, email="nokeyuser@example.com")
    response = client.get(url_for('keys.manage_api_keys_page'))
    assert response.status_code == 200
    assert b"Gestione Chiavi API" in response.data
    assert b"Non hai ancora generato nessuna chiave API." in response.data

# --- Test per la Generazione di Chiavi ---
def test_generate_api_key(client, app):
    """Testa la generazione di una nuova chiave API."""
    user_id = register_and_login_for_api_keys(client, app, email="genkeyuser@example.com")

    # Invia POST per generare una chiave
    key_name = "Test Key Alpha"
    response_generate = client.post(url_for('keys.generate_api_key_action'), data={
        'key_name': key_name
    }, follow_redirects=True) # Segue il redirect a manage_api_keys_page

    assert response_generate.status_code == 200
    assert b"Gestione Chiavi API" in response_generate.data # Torna alla pagina di gestione
    assert b"Nuova chiave API generata con successo!" in response_generate.data
    assert key_name.encode('utf-8') in response_generate.data # Verifica che il nome sia mostrato

    # Verifica che la chiave sia nel database
    new_key_value = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT key, name FROM api_keys WHERE user_id = ? AND name = ?", (user_id, key_name))
        key_row = cursor.fetchone()
        conn.close()
        assert key_row is not None, "Chiave API non trovata nel DB dopo generazione"
        assert key_row[1] == key_name
        new_key_value = key_row[0]
        assert new_key_value.startswith("sk_") # Verifica prefisso

    assert new_key_value is not None
    # Verifica che la chiave generata sia nel messaggio flash (e quindi nella pagina)
    assert new_key_value.encode('utf-8') in response_generate.data


# --- Test per l'Eliminazione di Chiavi ---
def test_delete_api_key(client, app):
    """Testa l'eliminazione di una chiave API."""
    user_id = register_and_login_for_api_keys(client, app, email="delkeyuser@example.com")
    key_name_to_delete = "Key To Delete"

    # 1. Genera una chiave da eliminare
    client.post(url_for('keys.generate_api_key_action'), data={'key_name': key_name_to_delete})

    # Recupera l'ID della chiave appena creata dal DB
    key_id_to_delete = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM api_keys WHERE user_id = ? AND name = ?", (user_id, key_name_to_delete))
        key_row = cursor.fetchone()
        conn.close()
        assert key_row is not None, "Chiave da eliminare non trovata nel DB"
        key_id_to_delete = key_row[0]

    # 2. Invia la richiesta DELETE all'endpoint API
    # Nota: url_for per endpoint con parametri: url_for('blueprint.route_function', param_name=value)
    delete_url = url_for('keys.delete_api_key_action', key_id=key_id_to_delete)
    response_delete = client.delete(delete_url) # client.delete per richieste DELETE

    assert response_delete.status_code == 200
    delete_data = response_delete.json
    assert delete_data['success'] is True
    assert "Chiave API eliminata con successo" in delete_data['message']

    # 3. Verifica che la chiave sia stata rimossa dal DB
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM api_keys WHERE id = ?", (key_id_to_delete,))
        key_row_after_delete = cursor.fetchone()
        conn.close()
        assert key_row_after_delete is None, "Chiave API ancora presente nel DB dopo eliminazione"

def test_delete_api_key_unauthorized(client, app):
    """Testa il tentativo di eliminare una chiave non propria o inesistente."""
    # --- Registra e logga il primo utente (owner) ---
    user_id_owner = register_and_login_for_api_keys(client, app, email="owner@example.com", password="password")

    # Genera una chiave per l'utente "owner"
    client.post(url_for('keys.generate_api_key_action'), data={'key_name': "Owner's Key"})
    key_id_owner = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE']); cursor = conn.cursor()
        cursor.execute("SELECT id FROM api_keys WHERE user_id=?", (user_id_owner,)); key_id_owner = cursor.fetchone()[0]; conn.close()

    # --- EFFETTUA IL LOGOUT DEL PRIMO UTENTE ---
    client.get(url_for('logout'), follow_redirects=True)
    logger.info("TEST: Eseguito logout di 'owner@example.com' prima di registrare 'otheruser'")
    # Verifica che la sessione sia pulita (opzionale ma buono per debug)
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is None, "La sessione non è stata pulita dopo il logout di owner"
    # -----------------------------------------

    # Ora logga un altro utente ("otheruser")
    # Dato che il client è stato sloggato, la registrazione di otheruser dovrebbe funzionare
    user_id_other = register_and_login_for_api_keys(client, app, email="otheruser@example.com", password="password")
    assert user_id_other is not None, "Fallimento registrazione/login di otheruser" # Aggiungi un'asserzione qui

    # "otheruser" tenta di eliminare la chiave di "owner"
    delete_url = url_for('keys.delete_api_key_action', key_id=key_id_owner)
    response_delete_other = client.delete(delete_url)
    assert response_delete_other.status_code == 404
    delete_data_other = response_delete_other.json
    assert delete_data_other['success'] is False

    # Tenta di eliminare una chiave con ID inesistente (otheruser è ancora loggato)
    delete_url_non_existent = url_for('keys.delete_api_key_action', key_id=99999)
    response_delete_non_existent = client.delete(delete_url_non_existent)
    assert response_delete_non_existent.status_code == 404
    delete_data_non_existent = response_delete_non_existent.json
    assert delete_data_non_existent['success'] is False


# --- Test per la Verifica di Chiavi (opzionale, dato che @require_api_key è più un test di integrazione) ---
# Questi test sono più complessi perché richiedono di inviare l'header X-API-Key
# e di avere TestConfig.APP_MODE = 'saas' per testare il flusso corretto di @require_api_key.
# Se TestConfig.APP_MODE = 'single', @require_api_key non fa nulla.

@pytest.mark.skipif(lambda config: config.get('APP_MODE') != 'saas', reason="API Key verification test only runs in SAAS mode")
def test_verify_api_key_endpoint_valid(client, app):
    # Questo test richiede TestConfig.APP_MODE = 'saas'
    # 1. Registra un utente e genera una chiave per lui programmaticamente
    user_id = None
    api_key_value = "sk_test_valid_key_for_verification_123" # Chiave fittizia
    key_name = "Verify Test Key"

    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        # Crea utente
        temp_email = "verifyuser@example.com"
        temp_user_id = User.generate_id()
        cursor.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
                       (temp_user_id, temp_email, User(id=None, email=None).set_password("test"))) # Password hash fittizio
        # Crea API Key
        cursor.execute("INSERT INTO api_keys (user_id, key, name) VALUES (?, ?, ?)",
                       (temp_user_id, api_key_value, key_name))
        conn.commit()
        user_id = temp_user_id # Salva l'user_id per asserzioni future se necessario
        conn.close()

    assert user_id is not None

    # 2. Chiama l'endpoint /keys/api/verify con la chiave nell'header
    headers = {'X-API-Key': api_key_value}
    response = client.get(url_for('keys.verify_api_key_endpoint'), headers=headers)

    assert response.status_code == 200
    data = response.json
    assert data['success'] is True
    assert "valida" in data['message'].lower()

@pytest.mark.skipif(lambda config: config.get('APP_MODE') != 'saas', reason="API Key verification test only runs in SAAS mode")
def test_verify_api_key_endpoint_invalid(client, app):
    # Questo test richiede TestConfig.APP_MODE = 'saas'
    headers = {'X-API-Key': 'sk_invalid_key_does_not_exist'}
    response = client.get(url_for('keys.verify_api_key_endpoint'), headers=headers)

    assert response.status_code == 401 # Unauthorized
    data = response.json
    assert data['success'] is False
    assert data['error_code'] == 'UNAUTHORIZED'
