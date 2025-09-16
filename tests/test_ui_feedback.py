import pytest
from unittest.mock import patch, MagicMock
from flask import url_for
import sqlite3

# Helper aggiornato per usare monkeypatch
def login_test_user_for_feedback(client, app, monkeypatch, email="uitest@example.com", password="password"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'email': email, 'password': password, 'confirm_password': password})
    client.post(url_for('login'), data={'email': email, 'password': password})
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
        conn.close()
    assert user_id is not None, f"Fallimento creazione utente {email}"
    return user_id

# Helper per creare un video di test nel database
def create_test_video_for_feedback(app, user_id, video_id="test_vid_ui_01"):
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO videos (video_id, title, url, channel_id, published_at, user_id, processing_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (video_id, "Test Video for UI", "http://fake.url", "fake_channel", "2023-01-01T00:00:00Z", user_id, "completed"))
        conn.commit()
        conn.close()
    return video_id


def test_reprocess_failure_shows_banner_message(client, app, monkeypatch):
    """
    TEST: Verifica che, quando il riprocessamento di un video fallisce,
    il server restituisca un JSON di errore che il frontend userà per mostrare il banner.
    """
    # 1. ARRANGE
    user_id = login_test_user_for_feedback(client, app, monkeypatch)
    video_id = create_test_video_for_feedback(app, user_id)
    
    messaggio_di_errore_simulato = "Trascrizione non trovata a causa di un'eclissi solare."
    risultato_di_fallimento_simulato = {'error': 'TRANSCRIPT_FAILED', 'message': messaggio_di_errore_simulato}

    # --- MODIFICA CHIAVE E DEFINITIVA QUI ---
    # Il path corretto punta a dove la funzione è DEFINITA e da dove viene importata: 'app.main'
    path_load_credentials = 'app.main.load_credentials' 
    path_youtube_client = 'app.api.routes.videos.YouTubeClient'
    path_transcript_service = 'app.api.routes.videos.TranscriptService.get_transcript'
    
    mock_valid_credentials = MagicMock()
    mock_valid_credentials.valid = True

    with patch(path_load_credentials, return_value=mock_valid_credentials) as mock_creds, \
         patch(path_youtube_client, MagicMock()), \
         patch(path_transcript_service, return_value=risultato_di_fallimento_simulato) as mock_transcript_service:
        
        # 2. ACT
        response = client.post(url_for('videos.reprocess_single_video', video_id=video_id))

        # 3. ASSERT
        assert response.status_code == 200
        data = response.json
        assert data['success'] is False
        assert data['error_code'] == 'TRANSCRIPT_FAILED' 
        assert messaggio_di_errore_simulato in data['message']
        
        mock_creds.assert_called_once()
        mock_transcript_service.assert_called_once()