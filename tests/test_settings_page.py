import pytest
import sqlite3
from flask import url_for

# Funzione helper per creare e loggare un utente di test
def setup_settings_user(client, app, monkeypatch, email="settings_user@test.com"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'email': email, 'password': 'password', 'confirm_password': 'password'})
    client.post(url_for('login'), data={'email': email, 'password': 'password'})
    
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
        conn.close()
    assert user_id is not None
    return user_id

def test_settings_page_saves_data_correctly(client, app, monkeypatch):
    """
    TEST 1: Verifica che una richiesta POST a /settings salvi i dati nel database.
    """
    # 1. ARRANGE: Prepara l'ambiente e i dati di test
    user_id = setup_settings_user(client, app, monkeypatch)
    
    test_settings_data = {
        'llm_provider': 'ollama',
        'ollama_base_url': 'http://localhost:11434',
        'ollama_model_name': 'test-model',
        'wordpress_url': 'https://test.blog',
        'wordpress_username': 'wp_user',
        'wordpress_api_key': 'app_password_123'
    }

    # 2. ACT: Esegui la richiesta POST per salvare le impostazioni
    response = client.post(
        url_for('settings.settings_page'), 
        data=test_settings_data,
        follow_redirects=True
    )

    # 3. ASSERT: Controlla la risposta e il database
    assert response.status_code == 200
    assert b"Impostazioni salvate con successo!" in response.data

    # Verifica diretta sul database
    saved_settings = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
        saved_settings = cursor.fetchone()
        conn.close()

    assert saved_settings is not None
    assert saved_settings['llm_provider'] == 'ollama'
    assert saved_settings['llm_model_name'] == 'test-model'
    assert saved_settings['wordpress_url'] == 'https://test.blog'
    assert saved_settings['wordpress_api_key'] == 'app_password_123'

def test_settings_page_loads_data_correctly(client, app, monkeypatch):
    """
    TEST 2: Verifica che una richiesta GET a /settings carichi e mostri
    i dati precedentemente salvati nel database.
    """
    # 1. ARRANGE: Prepara l'utente e inserisci manualmente dei dati nel DB
    user_id = setup_settings_user(client, app, monkeypatch, email="load_settings@test.com")
    
    test_settings_to_load = {
        'llm_provider': 'google',
        'llm_model_name': 'gemini-pro,gemini-flash', # Test con modello primario e fallback
        'llm_api_key': 'secret_google_key'
    }

    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_settings (user_id, llm_provider, llm_model_name, llm_api_key) VALUES (?, ?, ?, ?)",
            (user_id, test_settings_to_load['llm_provider'], test_settings_to_load['llm_model_name'], test_settings_to_load['llm_api_key'])
        )
        conn.commit()
        conn.close()

    # 2. ACT: Esegui la richiesta GET per caricare la pagina
    response = client.get(url_for('settings.settings_page'))

    # 3. ASSERT: Controlla che i dati siano presenti nell'HTML della pagina
    assert response.status_code == 200
    # Controlliamo che il provider 'google' sia selezionato nel menu a tendina (anche se è il default)
    assert b'<div data-value="google">' in response.data
    # Verifichiamo che i modelli siano stati splittati e inseriti nei campi giusti
    assert b'value="gemini-pro"' in response.data
    assert b'value="gemini-flash"' in response.data
    # Controlliamo che la chiave API sia presente nel campo (anche se di tipo password)
    assert b'value="secret_google_key"' in response.data