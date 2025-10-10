import pytest
import sqlite3
from unittest.mock import patch, MagicMock
from flask import url_for
from app.api.models.video import Video

# Funzioni helper (invariate)
def login_test_user_for_videos(client, app, monkeypatch, email="videouser@example.com"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'email': email, 'password': 'password', 'confirm_password': 'password'})
    client.post(url_for('login'), data={'email': email, 'password': 'password'})
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

def create_test_video_in_db(app, user_id, video_id="testvid001", initial_status="completed"):
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO videos (video_id, title, url, channel_id, published_at, user_id, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (video_id, "Test Video", f"http://fake.url/{video_id}", "fake_channel", "2023-01-01T00:00:00Z", user_id, initial_status)
        )
        conn.commit()
        conn.close()
    return video_id

def test_reprocess_single_video_success(client, app, monkeypatch):
    """
    Testa POST /api/videos/<video_id>/reprocess con successo. (Versione Definitiva Corretta)
    """
    # 1. ARRANGE
    user_id = login_test_user_for_videos(client, app, monkeypatch, email="reprocess_user_v3@example.com")
    video_id = create_test_video_in_db(app, user_id, video_id="vid_reprocess_03")
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("UPDATE videos SET title = ? WHERE video_id = ?", ('Titolo &quot;Sporco&quot;', video_id))
        conn.commit()
        conn.close()

    mock_transcript_result = {'text': "Trascrizione aggiornata.", 'language': 'it', 'type': 'manual'}
    mock_video_model = Video(video_id=video_id, title="Titolo Pulito", description='Descrizione pulita.', url='http://fake.url', channel_id='fake_channel', published_at='2023-01-01T00:00:00Z')
    mock_chunks = ["Trascrizione aggiornata."]
    mock_embeddings = [[0.5] * 768]
    
    path_load_credentials = 'app.main.load_credentials'
    path_get_video_details = 'app.api.routes.videos.YouTubeClient.get_video_details'
    path_transcript_service = 'app.api.routes.videos.TranscriptService.get_transcript'
    path_split_chunks = 'app.api.routes.videos.split_text_into_chunks'
    path_generate_embeddings = 'app.api.routes.videos.generate_embeddings'
    path_agentic_chunker = 'app.api.routes.videos.chunk_text_agentically'
    
    mock_valid_credentials = MagicMock(valid=True)
    mock_chroma_collection = MagicMock()
    mock_chroma_client = MagicMock()
    mock_chroma_client.get_or_create_collection.return_value = mock_chroma_collection

    with patch(path_load_credentials, return_value=mock_valid_credentials), \
         patch(path_get_video_details, return_value=mock_video_model), \
         patch(path_transcript_service, return_value=mock_transcript_result), \
         patch(path_split_chunks, return_value=mock_chunks), \
         patch(path_generate_embeddings, return_value=mock_embeddings), \
         patch(path_agentic_chunker, return_value=mock_chunks), \
         patch.dict(app.config, {'CHROMA_CLIENT': mock_chroma_client}):
        
        # 2. ACT
        response = client.post(url_for('videos.reprocess_single_video', video_id=video_id))

    # 3. ASSERT
    assert response.status_code == 200
    data = response.json
    assert data['success'] is True
    assert data['new_status'] == 'completed'
    assert data['updated_metadata']['title'] == "Titolo Pulito"

    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT title FROM videos WHERE video_id = ?", (video_id,))
        final_title = cursor.fetchone()[0]
        conn.close()
    
    assert final_title == "Titolo Pulito"