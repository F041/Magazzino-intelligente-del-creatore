import pytest
import time
from unittest.mock import patch, MagicMock, ANY
from flask import url_for
import sqlite3
from app.api.routes import rss as rss_api # Importiamo il modulo per accedere alle sue variabili

def setup_function(function):
    """Questa funzione viene eseguita prima di ogni test in questo file."""
    # Resettiamo lo stato del processo RSS per garantire che ogni test parta pulito.
    with rss_api.rss_status_lock:
        rss_api.rss_processing_status['is_processing'] = False
        rss_api.rss_processing_status['error'] = None

# Helper (invariato e corretto)
def login_test_user_for_rss(client, app, monkeypatch, email="rssuser@example.com", password="password"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'email': email, 'password': password, 'confirm_password': password})
    client.post(url_for('login'), data={'email': email, 'password': password})
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row: user_id = user_row[0]
        conn.close()
    assert user_id is not None, f"Fallimento nella creazione dell'utente {email}"
    return user_id

# Test principale (con la logica di verifica corretta)
def test_process_rss_feed_api_starts_background_job(client, app, monkeypatch):
    """
    Testa che POST /api/rss/process avvii il processo in background.
    """
    user_id_registrato = login_test_user_for_rss(client, app, monkeypatch, email="rssfeedtest@example.com")
    test_rss_url = "https://www.example.com/testfeed.xml"

    path_process_rss_core = 'app.api.routes.rss._process_rss_feed_core'
    with patch(path_process_rss_core, return_value=True) as mock_core_rss_processor:
        response = client.post(url_for('rss.process_rss_feed'), json={
            'rss_url': test_rss_url
        })

        assert response.status_code == 202
        assert response.json['success'] is True

        time.sleep(0.5)

        # --- INIZIO BLOCCO DI VERIFICA MODIFICATO ---
        
        # 1. Verifica che la funzione sia stata chiamata una sola volta.
        mock_core_rss_processor.assert_called_once()

        # 2. Estrai gli argomenti con cui è stata chiamata.
        args, kwargs = mock_core_rss_processor.call_args
        
        # 3. Verifica gli argomenti. Ora ci aspettiamo SEMPRE 5 argomenti in totale
        # (3 posizionali e 2 nominati, come abbiamo visto prima del refactoring).
        # L'argomento user_id (il secondo posizionale, indice 1) deve essere quello dell'utente loggato.
        
        # Verifiche più specifiche e chiare
        assert args[0] == test_rss_url
        assert args[1] == user_id_registrato # L'asserzione chiave che ora deve passare
        assert isinstance(args[2], dict) # Il dizionario core_config
        assert 'status_dict' in kwargs
        assert 'status_lock' in kwargs
        # --- FINE BLOCCO DI VERIFICA MODIFICATO ---

def test_process_rss_feed_api_invalid_url(client, app, monkeypatch):
    """Testa l'API /api/rss/process con un URL non valido."""
    login_test_user_for_rss(client, app, monkeypatch, email="rssinvalidurl@example.com")
    response = client.post(url_for('rss.process_rss_feed'), json={'rss_url': "non-un-url-valido"})
    assert response.status_code == 400
    assert response.json['success'] is False

def test_process_rss_feed_api_missing_url(client, app, monkeypatch):
    """Testa l'API /api/rss/process senza fornire rss_url."""
    login_test_user_for_rss(client, app, monkeypatch, email="rssmissingurl@example.com")
    response = client.post(url_for('rss.process_rss_feed'), json={})
    assert response.status_code == 400
    assert response.json['success'] is False