import pytest
from unittest.mock import patch, MagicMock
from flask import url_for
import sqlite3
from app.api.models.video import Video # <-- NUOVO IMPORT

# Funzioni helper (invariate)
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
        if user_row: user_id = user_row[0]
        conn.close()
    assert user_id is not None
    return user_id

def create_test_video_for_feedback(app, user_id, video_id="test_vid_ui_01"):
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO videos (video_id, title, url, channel_id, published_at, user_id, processing_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (video_id, "Test Video for UI", "http://fake.url", "fake_channel", "2023-01-01T00:00:00Z", user_id, "completed"))
        conn.commit()
        conn.close()
    return video_id

def test_reprocess_failure_shows_banner_message(client, app, monkeypatch):
    """
    TEST CORRETTO: Verifica la gestione del fallimento della trascrizione.
    """
    # 1. ARRANGE
    user_id = login_test_user_for_feedback(client, app, monkeypatch)
    video_id = create_test_video_for_feedback(app, user_id)
    
    messaggio_di_errore_simulato = "Trascrizione non trovata a causa di un'eclissi solare."
    risultato_di_fallimento_simulato = {'error': 'TRANSCRIPT_FAILED', 'message': messaggio_di_errore_simulato}

    # Creiamo un finto oggetto Video per simulare la risposta di get_video_details
    mock_video_model = Video(video_id=video_id, title='Titolo Test', description='Desc Test', url='http://fake.url', channel_id='fake_channel', published_at='2023-01-01T00:00:00Z')

    path_load_credentials = 'app.main.load_credentials' 
    path_get_video_details = 'app.api.routes.videos.YouTubeClient.get_video_details'
    path_transcript_service = 'app.api.routes.videos.TranscriptService.get_transcript'
    
    mock_valid_credentials = MagicMock(valid=True)
    # Inganniamo os.path.exists per l'init del client
    monkeypatch.setattr("os.path.exists", lambda path: True)

    with patch(path_load_credentials, return_value=mock_valid_credentials), \
         patch(path_get_video_details, return_value=mock_video_model) as mock_details, \
         patch(path_transcript_service, return_value=risultato_di_fallimento_simulato) as mock_transcript:
        
        # 2. ACT
        response = client.post(url_for('videos.reprocess_single_video', video_id=video_id))

    # 3. ASSERT
    assert response.status_code == 200
    data = response.json
    
    # Ora l'asserzione corretta passerà, perché il DB non andrà in errore
    assert data['success'] is False
    assert data['error_code'] == 'TRANSCRIPT_FAILED' 
    assert messaggio_di_errore_simulato in data['message']
    
    # Verifichiamo che le nostre spie siano state chiamate
    mock_details.assert_called_once()
    mock_transcript.assert_called_once()