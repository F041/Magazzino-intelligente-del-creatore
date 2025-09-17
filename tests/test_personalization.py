import pytest
import sqlite3
from flask import url_for

# --- Funzione Helper Specifica per questo Test ---
# Ci serve per registrare, loggare e recuperare l'ID di un utente di test.
def setup_test_user(client, app, monkeypatch, email="personalization@example.com"):
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
    assert user_id is not None, "La creazione dell'utente di test è fallita."
    return user_id

# --- Test #1: La pagina si carica correttamente ---
def test_personalization_page_loads(client, app, monkeypatch):
    """Verifica che la pagina /personalization risponda con 200 OK per un utente loggato."""
    setup_test_user(client, app, monkeypatch)
    
    response = client.get(url_for('personalization_page'))
    
    assert response.status_code == 200
    assert b"Branding e aspetto" in response.data # Corretto 'Aspetto' in 'aspetto'
    
# --- Test #2: Il salvataggio dei dati funziona ---
def test_personalization_save_post(client, app, monkeypatch):
    """Simula un utente che compila il form e clicca 'Salva', poi verifica il database."""
    user_id = setup_test_user(client, app, monkeypatch, email="save_settings@example.com")

    # Dati di test che invieremo con il form
    test_data = {
        'brand_color': '#ff0000',
        'brand_logo_url': 'https://example.com/logo.png',
        'welcome_message': 'Benvenuto di test!',
        'prompt_starter_1': 'Domanda 1',
        'prompt_starter_2': 'Domanda 2',
        'prompt_starter_3': '' # Lasciamo uno vuoto volutamente
    }

    # Eseguiamo la richiesta POST, seguendo il redirect
    response = client.post(url_for('personalization_page'), data=test_data, follow_redirects=True)

    # Verifichiamo che l'operazione sia andata a buon fine
    assert response.status_code == 200
    assert b"Impostazioni di personalizzazione salvate con successo!" in response.data

    # La verifica più importante: controlliamo direttamente il database!
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
        settings_row = cursor.fetchone()
        conn.close()

        assert settings_row is not None
        assert settings_row['brand_color'] == '#ff0000'
        assert settings_row['welcome_message'] == 'Benvenuto di test!'
        assert settings_row['prompt_starter_1'] == 'Domanda 1'
        assert settings_row['prompt_starter_3'] == '' # Deve aver salvato la stringa vuota

# --- Test #3: Il ripristino dei dati funziona ---
def test_personalization_reset(client, app, monkeypatch):
    """Salva dei dati e poi verifica che il ripristino li cancelli."""
    user_id = setup_test_user(client, app, monkeypatch, email="reset_settings@example.com")

    # Prima, salviamo dei dati (usiamo la stessa logica del test precedente)
    client.post(url_for('personalization_page'), data={'welcome_message': 'Da cancellare'})

    # Ora, visitiamo la pagina di ripristino
    response = client.get(url_for('reset_personalization'), follow_redirects=True)
    
    assert response.status_code == 200
    assert b"Le impostazioni di personalizzazione sono state ripristinate." in response.data

    # Verifichiamo il database: i campi dovrebbero essere NULL
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT welcome_message FROM user_settings WHERE user_id = ?", (user_id,))
        settings_row = cursor.fetchone()
        conn.close()

        assert settings_row is not None
        assert settings_row['welcome_message'] is None # La prova che il reset ha funzionato