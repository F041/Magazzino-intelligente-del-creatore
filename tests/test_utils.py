import pytest
import sqlite3
from unittest.mock import patch, MagicMock
from flask import url_for
from app.utils import build_full_config_for_background_process

# --- NUOVA FUNZIONE HELPER (ROBUSTA E CORRETTA) ---
# Questa funzione si occupa di creare e loggare un utente finto
# per assicurarsi che il contesto di Flask sia corretto.
@pytest.fixture
def logged_in_user(client, app, monkeypatch):
    email = "utilstest@example.com"
    password = "password"
    
    # Imposta l'email permessa per la registrazione
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    
    # Registra e logga l'utente
    client.post(url_for('register'), data={'email': email, 'password': password, 'confirm_password': password})
    client.post(url_for('login'), data={'email': email, 'password': password})
    
    # Recupera l'ID dell'utente appena creato
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
    return user_id # Restituisce l'ID dell'utente loggato

def test_build_config_with_user_overrides(app, client, logged_in_user):
    """
    TEST SCENARIO 1: Verifica che le impostazioni di un utente (Ollama)
    sovrascrivano correttamente quelle di default (Google).
    'logged_in_user' è la nostra nuova fixture che prepara l'ambiente.
    """
    # 1. ARRANGE
    app.config['llm_provider'] = 'google'
    app.config['RAG_MODELS_LIST'] = ['gemini-default']
    
    mock_db_row = {
        'llm_provider': 'ollama',
        'llm_model_name': 'llama3:latest',
        'llm_embedding_model': 'nomic-embed-text',
        'llm_api_key': None,
        'ollama_base_url': 'http://ollama-host:11434'
    }
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = mock_db_row
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    
    with patch('app.utils.sqlite3.connect', return_value=mock_conn):
        # Usiamo il client per creare un contesto di richiesta REALE.
        # È questo che rende 'current_user' disponibile.
        with client:
            # 2. ACT
            full_config = build_full_config_for_background_process(user_id=logged_in_user)

    # 3. ASSERT
    assert full_config['llm_provider'] == 'ollama'
    assert full_config['RAG_MODELS_LIST'] == ['llama3:latest']
    assert full_config['ollama_base_url'] == 'http://ollama-host:11434'

def test_build_config_with_no_user_settings(app, client, logged_in_user):
    """
    TEST SCENARIO 2: Verifica che se un utente non ha impostazioni,
    vengano usate quelle di default.
    """
    # 1. ARRANGE
    app.config['llm_provider'] = 'google'
    app.config['RAG_MODELS_LIST'] = ['gemini-default']
    
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    
    with patch('app.utils.sqlite3.connect', return_value=mock_conn):
        with client:
            # 2. ACT
            full_config = build_full_config_for_background_process(user_id=logged_in_user)

    # 3. ASSERT
    assert full_config['llm_provider'] == 'google'
    assert full_config['RAG_MODELS_LIST'] == ['gemini-default']