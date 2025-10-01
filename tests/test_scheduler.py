import pytest
import sqlite3
from unittest.mock import patch, MagicMock, ANY
from flask import current_app

# Le funzioni helper rimangono le stesse, sono corrette.
def setup_test_monitoring_data(app, user_id):
    with app.app_context():
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM monitored_youtube_channels WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM monitored_rss_feeds WHERE user_id = ?", (user_id,))
        cursor.execute("INSERT OR IGNORE INTO monitored_youtube_channels (user_id, channel_id, is_active) VALUES (?, ?, ?)", (user_id, 'UC-test-channel', True))
        cursor.execute("INSERT OR IGNORE INTO monitored_rss_feeds (user_id, feed_url, is_active) VALUES (?, ?, ?)", (user_id, 'http://test.com/feed', True))
        conn.commit()
        conn.close()

def register_test_user_for_scheduler(app, monkeypatch, email="final_scheduler_user_v3@test.com"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    user_id = None
    with app.app_context():
        from app.models.user import User
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.commit()
        new_user = User(id=User.generate_id(), email=email)
        new_user.set_password('password')
        cursor.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)", (new_user.id, new_user.email, new_user.password_hash))
        conn.commit()
        user_id = new_user.id
        conn.close()
    assert user_id is not None
    return user_id

# --- INIZIO TEST DEFINITIVO ---
def test_scheduler_job_calls_correct_functions(app, monkeypatch):
    """
    Testa che il job dello scheduler chiami le funzioni corrette,
    mockando le dipendenze nei loro moduli di origine.
    """
    # 1. ARRANGE
    user_id = register_test_user_for_scheduler(app, monkeypatch)
    setup_test_monitoring_data(app, user_id)

    # Definiamo i path corretti puntando AI MODULI ORIGINALI.
    path_create_app = 'app.main.create_app' # create_app vive in app.main
    path_youtube_core = 'app.core.youtube_processor._process_youtube_channel_core' # La funzione core vive qui
    path_rss_core = 'app.api.routes.rss._process_rss_feed_core' # La funzione core vive qui
    path_youtube_client_class = 'app.services.youtube.client.YouTubeClient' # La classe client vive qui

    # Dati finti che i nostri mock restituiranno
    mock_video_list = [MagicMock()] 

    # Applichiamo tutte le nostre "spie" ai loro indirizzi reali
    with patch(path_create_app, return_value=app) as mock_create_app, \
         patch(path_youtube_core, return_value={'success': True}) as mock_yt_core, \
         patch(path_rss_core, return_value=True) as mock_rss_core, \
         patch(path_youtube_client_class) as MockYouTubeClient:

        # Configuriamo il comportamento della spia per YouTubeClient
        mock_yt_instance = MockYouTubeClient.return_value
        mock_yt_instance.get_channel_videos_and_total_count.return_value = (mock_video_list, 1)

        # Importiamo la funzione che vogliamo testare
        from app.scheduler_jobs import check_monitored_sources_job
        
        # 2. ACT
        # Eseguiamo la funzione del job.
        check_monitored_sources_job()

        # 3. ASSERT
        # Verifichiamo che tutto sia stato chiamato come ci aspettavamo
        mock_create_app.assert_called_once()
        mock_yt_core.assert_called_once()
        mock_rss_core.assert_called_once()
        
        # Le verifiche sugli argomenti ora funzioneranno perché stiamo
        # spiando le funzioni giuste nel posto giusto.
        mock_yt_core.assert_called_once_with(
            channel_id='UC-test-channel',
            user_id=user_id,
            core_config=ANY,
            videos_from_yt_models=mock_video_list,
            status_dict={},
            use_official_api_only=True
        )
        
        mock_rss_core.assert_called_once_with(
            'http://test.com/feed',
            user_id,
            ANY,
            None,
            None
        )