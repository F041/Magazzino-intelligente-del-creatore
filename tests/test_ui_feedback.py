import pytest
from unittest.mock import patch, MagicMock
from flask import url_for
import sqlite3
from app.api.models.video import Video
from google.api_core import exceptions as google_exceptions

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
        """, (video_id, "Test Video for UI", "http://fake.url/video", "fake_channel_id", "2023-01-01T00:00:00Z", user_id, "completed"))
        conn.commit()
        conn.close()
    return video_id

def test_reprocess_failure_shows_banner_message(client, app, monkeypatch):
    """
    Verifica la gestione del fallimento della trascrizione e il messaggio di errore corretto.
    """
    user_id = login_test_user_for_feedback(client, app, monkeypatch)
    video_id = create_test_video_for_feedback(app, user_id)
    
    messaggio_di_errore_simulato = "Trascrizione non trovata a causa di un'eclissi solare."
    risultato_di_fallimento_simulato = {'error': 'TRANSCRIPT_FAILED', 'message': messaggio_di_errore_simulato}

    mock_video_model = Video(
        video_id=video_id, title='Titolo Test', description='Desc Test',
        url='http://fake.url/test', channel_id='fake_channel_id', published_at='2023-01-01T00:00:00Z'
    )

    path_load_credentials = 'app.api.routes.videos.load_credentials'
    path_youtube_client_class = 'app.api.routes.videos.YouTubeClient'
    path_transcript_service_get_transcript = 'app.api.routes.videos.TranscriptService.get_transcript'
    
    mock_valid_credentials = MagicMock(valid=True)

    with patch(path_load_credentials, return_value=mock_valid_credentials), \
         patch(path_youtube_client_class) as MockYouTubeClient, \
         patch(path_transcript_service_get_transcript, return_value=risultato_di_fallimento_simulato):
        
        mock_yt_client_instance = MockYouTubeClient.return_value
        mock_yt_client_instance.get_video_details.return_value = mock_video_model

        response = client.post(url_for('videos.reprocess_single_video', video_id=video_id))

    assert response.status_code == 200
    data = response.json
    
    assert data['success'] is False
    assert data['error_code'] == 'TRANSCRIPT_FAILED' 
    assert messaggio_di_errore_simulato in data['message']

def test_youtube_processing_handles_quota_error_correctly(client, app, monkeypatch):
    """
    Verifica che il processo di analisi di un canale YouTube, quando incontra un errore di quota API,
    termini correttamente e imposti l'error_code 'QUOTA_EXCEEDED_SUGGEST_SCHEDULE'.
    """
    login_test_user_for_feedback(client, app, monkeypatch, email="quota_user@test.com")
    
    path_load_credentials = 'app.main.load_credentials'
    path_yt_client_class = 'app.core.youtube_processor.YouTubeClient'

    mock_valid_credentials = MagicMock(valid=True)
    mock_yt_client_instance = MagicMock()
    mock_yt_client_instance.get_channel_videos_and_total_count.side_effect = google_exceptions.ResourceExhausted("Simulated Google API Quota Exceeded")

    with patch(path_load_credentials, return_value=mock_valid_credentials), \
         patch(path_yt_client_class, return_value=mock_yt_client_instance):

        response_start = client.post(
            url_for('videos.process_channel'),
            json={'channel_url': 'https://www.youtube.com/channel/testchannel'}
        )
        assert response_start.status_code == 202

        import time
        time.sleep(1.5) # Aumentiamo leggermente per sicurezza

        response_progress = client.get(url_for('videos.get_progress'))

    assert response_progress.status_code == 200
    progress_data = response_progress.json
    
    assert progress_data['is_processing'] is False
    assert progress_data['error'] is not None
    assert progress_data['error_code'] == 'QUOTA_EXCEEDED_SUGGEST_SCHEDULE'
    assert "Limite richieste API raggiunto" in progress_data['message']