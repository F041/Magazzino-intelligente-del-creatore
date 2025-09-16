import pytest
import sqlite3
from flask import url_for, session
from app.models.user import User
import logging

logger = logging.getLogger(__name__)

# Helper aggiornato per usare monkeypatch e restituire l'ID utente in modo affidabile
def register_and_login_for_api_keys(client, app, monkeypatch, email):
    # Imposta l'email permessa per la registrazione
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    
    # Esegui la registrazione
    client.post(url_for('register'), data={
        'name': 'API Key User', 'email': email, 'password': 'password', 'confirm_password': 'password'
    }, follow_redirects=True)

    # Esegui il login
    client.post(url_for('login'), data={'email': email, 'password': 'password'}, follow_redirects=True)

    # Verifica che l'utente esista e recupera il suo ID
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
        conn.close()
    
    # L'asserzione ora è dentro l'helper, rendendo i test più puliti
    assert user_id is not None, f"Fallimento nella creazione/recupero dell'utente per l'email {email}"
    return user_id

# --- Test per la Pagina di Gestione Chiavi ---

def test_manage_api_keys_page_loads_unauthenticated(client):
    """Testa che un utente non loggato venga reindirizzato al login."""
    response = client.get(url_for('keys.manage_api_keys_page'), follow_redirects=True)
    assert response.status_code == 200
    assert b"Login" in response.data

def test_manage_api_keys_page_loads_authenticated_no_keys(client, app, monkeypatch):
    """Testa la pagina di gestione per un utente loggato senza chiavi API."""
    register_and_login_for_api_keys(client, app, monkeypatch, email="nokeyuser@example.com")
    response = client.get(url_for('keys.manage_api_keys_page'))
    assert response.status_code == 200
    assert b"Gestione chiavi API" in response.data
    assert b"Non hai ancora generato nessuna chiave API." in response.data

# --- Test per la Generazione di Chiavi ---

def test_generate_api_key(client, app, monkeypatch):
    """Testa la generazione di una nuova chiave API."""
    user_id = register_and_login_for_api_keys(client, app, monkeypatch, email="genkeyuser@example.com")

    key_name = "Test Key Alpha"
    response_generate = client.post(url_for('keys.generate_api_key_action'), data={
        'key_name': key_name
    }, follow_redirects=True)

    assert response_generate.status_code == 200
    assert b"Nuova chiave API generata con successo!" in response_generate.data
    assert key_name.encode('utf-8') in response_generate.data

    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT key, name FROM api_keys WHERE user_id = ? AND name = ?", (user_id, key_name))
        key_row = cursor.fetchone()
        conn.close()
        assert key_row is not None
        assert key_row[1] == key_name
        assert key_row[0].startswith("sk_")

# --- Test per l'Eliminazione di Chiavi ---

def test_delete_api_key(client, app, monkeypatch):
    """Testa l'eliminazione di una chiave API."""
    user_id = register_and_login_for_api_keys(client, app, monkeypatch, email="delkeyuser@example.com")
    key_name_to_delete = "Key To Delete"

    client.post(url_for('keys.generate_api_key_action'), data={'key_name': key_name_to_delete})

    key_id_to_delete = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM api_keys WHERE user_id = ? AND name = ?", (user_id, key_name_to_delete))
        key_row = cursor.fetchone()
        conn.close()
        assert key_row is not None
        key_id_to_delete = key_row[0]

    delete_url = url_for('keys.delete_api_key_action', key_id=key_id_to_delete)
    response_delete = client.delete(delete_url)

    assert response_delete.status_code == 200
    assert response_delete.json['success'] is True

    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM api_keys WHERE id = ?", (key_id_to_delete,))
        key_row_after_delete = cursor.fetchone()
        conn.close()
        assert key_row_after_delete is None

def test_delete_api_key_unauthorized(client, app, monkeypatch):
    """Testa il tentativo di eliminare una chiave non propria."""
    # Registra e logga il primo utente (owner)
    owner_id = register_and_login_for_api_keys(client, app, monkeypatch, email="owner@example.com")
    client.post(url_for('keys.generate_api_key_action'), data={'key_name': "Owner's Key"})
    key_id_owner = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE']); cursor = conn.cursor()
        cursor.execute("SELECT id FROM api_keys WHERE user_id=?", (owner_id,)); key_id_owner = cursor.fetchone()[0]; conn.close()

    # Esegui il logout
    client.get(url_for('logout'))

    # Registra e logga il secondo utente
    register_and_login_for_api_keys(client, app, monkeypatch, email="otheruser@example.com")

    # Il secondo utente prova a cancellare la chiave del primo
    delete_url = url_for('keys.delete_api_key_action', key_id=key_id_owner)
    response = client.delete(delete_url)
    assert response.status_code == 404 # Non trovato (perché non appartiene a questo utente)
    assert response.json['success'] is False