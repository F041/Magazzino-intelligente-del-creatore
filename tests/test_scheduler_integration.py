# In tests/test_scheduler_integration.py

import pytest
from unittest.mock import patch, MagicMock, ANY
import sqlite3
from flask import current_app
from app.api.models.video import Video

# --- Funzioni helper (MODIFICATE CON PULIZIA) ---
def setup_test_monitoring_data(app, user_id):
    with app.app_context():
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # PULIZIA PREVENTIVA per questo test
        cursor.execute("DELETE FROM monitored_youtube_channels WHERE user_id = ?", (user_id,))
        cursor.execute("INSERT INTO monitored_youtube_channels (user_id, channel_id, is_active) VALUES (?, ?, ?)", (user_id, 'UC-test-channel', True))
        conn.commit()
        conn.close()

def register_test_user_for_scheduler(app, monkeypatch, email="scheduler_user@example.com"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    user_id = None
    with app.app_context():
        from app.models.user import User
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # PULIZIA PREVENTIVA per questo test
        cursor.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.commit()
        
        new_user = User(id=User.generate_id(), email=email)
        new_user.set_password('password')
        
        cursor.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
            (new_user.id, new_user.email, new_user.password_hash)
        )
        conn.commit()
        conn.close()
        user_id = new_user.id
        
    assert user_id is not None
    return user_id

# --- Test principale (invariato) ---
@pytest.mark.skip(reason="Disattivato temporaneamente per problemi di configurazione ambiente in Docker.")
def test_scheduler_calls_processor_with_correct_data(app, monkeypatch):
    """
    Testa che il job dello scheduler chiami la funzione di processing
    con i dati corretti presi dal database.
    """
    # 1. ARRANGE
    user_id = register_test_user_for_scheduler(app, monkeypatch, email="integration@test.com")
    setup_test_monitoring_data(app, user_id)
    
    # ... il resto del test rimane identico ...
    path_youtube_processor = 'app.scheduler_jobs._process_youtube_channel_core'
    path_rss_processor = 'app.scheduler_jobs._process_rss_feed_core'
    path_youtube_client_class = 'app.services.youtube.client.YouTubeClient'
    mock_video_list = [MagicMock(spec=Video)]

    with patch(path_youtube_processor, return_value={'success': True}) as mock_youtube_core, \
         patch(path_rss_processor, return_value=True) as mock_rss_core, \
         patch(path_youtube_client_class) as MockYouTubeClient:
        
        mock_yt_instance = MockYouTubeClient.return_value
        mock_yt_instance.get_channel_videos_and_total_count.return_value = (mock_video_list, 1)

        from app.scheduler_jobs import check_monitored_sources_job
        
        # 2. ACT
        with app.app_context():
            check_monitored_sources_job()

        # 3. ASSERT
        mock_youtube_core.assert_called_once_with(
            channel_id='UC-test-channel',
            user_id=user_id,
            core_config=ANY,
            videos_from_yt_models=mock_video_list,
            status_dict=ANY,
            use_official_api_only=True
        )