import pytest
from unittest.mock import patch, MagicMock
from flask import url_for
import sqlite3
from app.api.models.video import Video

# Funzioni helper (aggiornate per coerenza)
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
        # Inseriamo tutti i campi necessari, compresi quelli di default e quelli non null
        cursor.execute("""
            INSERT OR REPLACE INTO videos (video_id, title, url, channel_id, published_at, user_id, processing_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (video_id, "Test Video for UI", "http://fake.url/video", "fake_channel_id", "2023-01-01T00:00:00Z", user_id, "completed"))
        conn.commit()
        conn.close()
    return video_id

def test_reprocess_failure_shows_banner_message(client, app, monkeypatch):
    """
    TEST CORRETTO: Verifica la gestione del fallimento della trascrizione
    e il messaggio di errore corretto.
    """
    # 1. ARRANGE
    user_id = login_test_user_for_feedback(client, app, monkeypatch)
    video_id = create_test_video_for_feedback(app, user_id)
    
    messaggio_di_errore_simulato = "Trascrizione non trovata a causa di un'eclissi solare."
    risultato_di_fallimento_simulato = {'error': 'TRANSCRIPT_FAILED', 'message': messaggio_di_errore_simulato}

    # Creiamo un finto oggetto Video per simulare la risposta di get_video_details
    mock_video_model = Video(
        video_id=video_id,
        title='Titolo Test',
        description='Desc Test',
        url='http://fake.url/test',
        channel_id='fake_channel_id',
        published_at='2023-01-01T00:00:00Z'
    )

    # Definiamo i path di TUTTE le funzioni e classi che dobbiamo "ingannare"
    path_load_credentials = 'app.api.routes.videos.load_credentials' # Ora è globale
    path_youtube_client_class = 'app.api.routes.videos.YouTubeClient'
    path_transcript_service_get_transcript = 'app.api.routes.videos.TranscriptService.get_transcript'
    
    mock_valid_credentials = MagicMock(valid=True)

    with patch(path_load_credentials, return_value=mock_valid_credentials), \
         patch(path_youtube_client_class) as MockYouTubeClient, \
         patch(path_transcript_service_get_transcript, return_value=risultato_di_fallimento_simulato):
        
        # Configura il mock di YouTubeClient
        mock_yt_client_instance = MockYouTubeClient.return_value
        mock_yt_client_instance.get_video_details.return_value = mock_video_model

        # 2. ACT
        response = client.post(url_for('videos.reprocess_single_video', video_id=video_id))

    # 3. ASSERT
    assert response.status_code == 200
    data = response.json
    
    assert data['success'] is False
    assert data['error_code'] == 'TRANSCRIPT_FAILED' 
    assert messaggio_di_errore_simulato in data['message']