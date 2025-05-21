import pytest
import time
from unittest.mock import patch, MagicMock, ANY
from flask import url_for
import sqlite3 # Per l'helper di login se necessario

# Helper per il login (puoi spostarlo in conftest.py se diventa comune)
def login_test_user_for_rss(client, app, email="rssuser@example.com", password="password"):
    client.post(url_for('register'), data={
        'email': email, 'password': password, 'confirm_password': password
    }, follow_redirects=True)
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row: user_id = user_row[0]
        conn.close()
    client.post(url_for('login'), data={'email': email, 'password': password}, follow_redirects=True)
    return user_id


def test_process_rss_feed_api_starts_background_job(client, app):
    """
    Testa che POST /api/rss/process avvii il processo in background.
    Mocka _process_rss_feed_core per evitare chiamate reali.
    """
    user_id = login_test_user_for_rss(client, app, email="rssfeedtest@example.com")
    test_rss_url = "https://www.example.com/testfeed.xml"

    # Path a _process_rss_feed_core nel modulo rss.py
    # Assumendo che _background_rss_processing chiami _process_rss_feed_core 
    # definito nello stesso modulo (app.api.routes.rss).
    path_process_rss_core = 'app.api.routes.rss._process_rss_feed_core'

    # Mock per la funzione core, simula successo
    with patch(path_process_rss_core, return_value=True) as mock_core_rss_processor:
        response = client.post(url_for('rss.process_rss_feed'), json={ # 'rss.' è il nome del blueprint
            'rss_url': test_rss_url
        })

        assert response.status_code == 202 # Accepted
        data = response.json
        assert data['success'] is True
        assert "avviata in background" in data['message'].lower()

        # Attendi un breve periodo per dare tempo al thread di avviarsi
        time.sleep(0.5) # Potrebbe necessitare aggiustamenti

        # Verifica che _process_rss_feed_core sia stato chiamato correttamente
        # L'user_id viene passato a _background_rss_processing, che lo passa a _process_rss_feed_core.
        # Il terzo argomento è il dizionario core_config.
        expected_user_id = user_id if app.config['APP_MODE'] == 'saas' else None
        mock_core_rss_processor.assert_called_once_with(test_rss_url, expected_user_id, ANY)

def test_process_rss_feed_api_invalid_url(client, app):
    """Testa l'API /api/rss/process con un URL non valido."""
    login_test_user_for_rss(client, app, email="rssinvalidurl@example.com")
    
    response = client.post(url_for('rss.process_rss_feed'), json={
        'rss_url': "non-un-url-valido"
    })
    
    assert response.status_code == 400 # Bad Request
    data = response.json
    assert data['success'] is False
    assert data.get('error_code') == 'VALIDATION_ERROR' # O il codice di errore specifico che hai impostato

def test_process_rss_feed_api_missing_url(client, app):
    """Testa l'API /api/rss/process senza fornire rss_url."""
    login_test_user_for_rss(client, app, email="rssmissingurl@example.com")
    
    response = client.post(url_for('rss.process_rss_feed'), json={}) # JSON vuoto
    
    assert response.status_code == 400 # Bad Request
    data = response.json
    assert data['success'] is False
    # Il messaggio di errore potrebbe variare leggermente a seconda della tua validazione
    assert "url" in (data.get('message','').lower() + data.get('error','').lower())


# Potremmo aggiungere un test per verificare il comportamento se un processo è già attivo (ALREADY_PROCESSING)
# ma richiederebbe di manipolare lo stato globale rss_processing_status, il che può essere
# un po' più complesso in un contesto di test e potrebbe richiedere un lock.