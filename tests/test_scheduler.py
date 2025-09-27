import pytest
from unittest.mock import patch, MagicMock, ANY
import sqlite3
from flask import current_app

# Helper per creare dati di test nel DB
def setup_test_monitoring_data(app, user_id):
    """Aggiunge un canale e un feed da monitorare per l'utente di test."""
    with app.app_context():
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Aggiungi un canale YouTube attivo
        cursor.execute("""
            INSERT INTO monitored_youtube_channels (user_id, channel_id, is_active)
            VALUES (?, ?, ?)
        """, (user_id, 'UC-test-channel', True))

        # Aggiungi un feed RSS attivo
        cursor.execute("""
            INSERT INTO monitored_rss_feeds (user_id, feed_url, is_active)
            VALUES (?, ?, ?)
        """, (user_id, 'http://test.com/feed', True))

        conn.commit()
        conn.close()

# Helper per registrare un utente (lo prendiamo da un altro test)
def register_test_user_for_scheduler(app, monkeypatch, email="scheduler_user@example.com"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    # Questa funzione non ha bisogno del client, opera direttamente sull'app
    # per recuperare l'ID utente in modo affidabile
    user_id = None
    with app.app_context():
        # Dobbiamo importare User qui dentro per evitare problemi di contesto
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

def test_scheduler_job_calls_processing_functions(app, monkeypatch):
    """
    Testa che il job dello scheduler recuperi i dati e chiami le funzioni corrette.
    """
    # 1. ARRANGE
    user_id = register_test_user_for_scheduler(app, monkeypatch)
    setup_test_monitoring_data(app, user_id)

    # Path delle funzioni che vogliamo spiare
    path_youtube_processor = 'app.scheduler_jobs._process_youtube_channel_core'
    path_rss_processor = 'app.scheduler_jobs._process_rss_feed_core'
    
    # --- LA CORREZIONE DEFINITIVA ---
    # Puntiamo direttamente alla CLASSE ORIGINALE nella sua "fabbrica".
    # Qualsiasi parte del codice che la importerà, riceverà la nostra versione finta.
    path_youtube_client_class = 'app.services.youtube.client.YouTubeClient'

    # Dati finti che il nostro finto client restituirà
    mock_video_list = [MagicMock()]

    # Usiamo 'patch' per sostituire TUTTE le dipendenze esterne
    with patch(path_youtube_processor, return_value={'success': True}) as mock_youtube_func, \
         patch(path_rss_processor, return_value=True) as mock_rss_func, \
         patch(path_youtube_client_class) as MockYouTubeClient:

        # Configuriamo la nostra "spia":
        mock_yt_instance = MockYouTubeClient.return_value
        mock_yt_instance.get_channel_videos_and_total_count.return_value = (mock_video_list, 1)
        
        # Importiamo la funzione del job
        from app.scheduler_jobs import check_monitored_sources_job
        
        # 2. ACT
        with app.app_context():
            check_monitored_sources_job()

        # 3. ASSERT
        
        # Verifichiamo che il metodo del nostro client finto sia stato chiamato
        mock_yt_instance.get_channel_videos_and_total_count.assert_called_once_with('UC-test-channel')
        
        # Ora queste verifiche, che prima fallivano, funzioneranno
        mock_youtube_func.assert_called_once()
        mock_rss_func.assert_called_once()
        
        mock_youtube_func.assert_called_once_with(
            channel_id='UC-test-channel',
            user_id=user_id,
            core_config=ANY,
            videos_from_yt_models=mock_video_list,
            status_dict={},
            use_official_api_only=True
        )

        rss_call_args = mock_rss_func.call_args[0]
        assert rss_call_args[0] == 'http://test.com/feed'
        assert rss_call_args[1] == user_id