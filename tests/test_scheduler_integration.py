import pytest
from unittest.mock import patch, MagicMock
import sqlite3
from flask import current_app

# Importiamo il modello Video per creare dati fittizi
from app.api.models.video import Video

# --- INIZIO FUNZIONI HELPER COPIATE ---
# Queste funzioni sono state copiate da test_scheduler.py per evitare problemi di import.

def setup_test_monitoring_data(app, user_id):
    """Aggiunge un canale e un feed da monitorare per l'utente di test."""
    with app.app_context():
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO monitored_youtube_channels (user_id, channel_id, is_active)
            VALUES (?, ?, ?)
        """, (user_id, 'UC-test-channel', True))

        cursor.execute("""
            INSERT INTO monitored_rss_feeds (user_id, feed_url, is_active)
            VALUES (?, ?, ?)
        """, (user_id, 'http://test.com/feed', True))

        conn.commit()
        conn.close()

def register_test_user_for_scheduler(app, monkeypatch, email="scheduler_user@example.com"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    user_id = None
    with app.app_context():
        from app.models.user import User
        
        new_user = User(id=User.generate_id(), email=email)
        new_user.set_password('password')
        
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
            (new_user.id, new_user.email, new_user.password_hash)
        )
        conn.commit()
        conn.close()
        user_id = new_user.id
        
    assert user_id is not None
    return user_id

# --- FINE FUNZIONI HELPER COPIATE ---


def test_scheduler_integration_happy_path(app, monkeypatch):
    """
    Test di integrazione per lo scheduler.
    Verifica il flusso completo, mockando solo le chiamate esterne a YouTube.
    """
    # --- 1. ARRANGE (Prepara l'ambiente) ---

    # a) Crea utente e dati di monitoraggio nel DB di test
    user_id = register_test_user_for_scheduler(app, monkeypatch, email="integration@test.com")
    setup_test_monitoring_data(app, user_id)

    # b) Dati FITTIZI che il nostro "finto" YouTubeClient restituirà
    mock_video_from_yt = Video(
        video_id='vid-new-01',
        title='Video Nuovo di Test',
        url='http://fake.url/vid-new-01',
        channel_id='UC-test-channel',
        published_at='2025-01-01T00:00:00Z',
        description='Descrizione test.'
    )
    mock_transcript_result = {
        'text': 'Questa è la trascrizione del video di test.',
        'language': 'it',
        'type': 'manual'
    }

    # c) Prepariamo i nostri "attori" (i mock)
    mock_yt_client_instance = MagicMock()
    mock_yt_client_instance.get_channel_videos.return_value = [mock_video_from_yt]
    
    mock_transcript_service = MagicMock()
    mock_transcript_service.get_transcript.return_value = mock_transcript_result

    mock_embeddings = [[0.1] * 768]

    # d) Definiamo i path delle classi/funzioni da sostituire
    path_youtube_client_class = 'app.core.youtube_processor.YouTubeClient'
    path_transcript_service_class = 'app.core.youtube_processor.TranscriptService'
    path_generate_embeddings = 'app.core.youtube_processor.generate_embeddings'
    
    with patch(path_youtube_client_class, return_value=mock_yt_client_instance) as mock_yt_class, \
         patch(path_transcript_service_class, mock_transcript_service), \
         patch(path_generate_embeddings, return_value=mock_embeddings):
        
        from app.scheduler_jobs import check_monitored_sources_job
        
        # --- 2. ACT (Esegui l'azione) ---
        with app.app_context():
            check_monitored_sources_job()

    # --- 3. ASSERT (Verifica i risultati) ---
    with app.app_context():
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM videos WHERE video_id = ?", ('vid-new-01',))
        video_row = cursor.fetchone()
        assert video_row is not None
        assert video_row['title'] == 'Video Nuovo di Test'
        assert video_row['processing_status'] == 'completed'

        chroma_client = current_app.config.get('CHROMA_CLIENT')
        collection_name = f"video_transcripts_{user_id}" if current_app.config.get('APP_MODE') == 'saas' else "video_transcripts"
        collection = chroma_client.get_collection(name=collection_name)
        
        results_in_chroma = collection.get(where={'video_id': 'vid-new-01'})
        assert len(results_in_chroma['ids']) > 0
        
        conn.close()