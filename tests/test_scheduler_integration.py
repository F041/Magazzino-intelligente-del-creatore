import pytest
from unittest.mock import patch, MagicMock
import sqlite3
from flask import current_app
from app.api.models.video import Video

# --- Funzioni helper (invariate) ---
def setup_test_monitoring_data(app, user_id):
    with app.app_context():
        db_path = current_app.config.get('DATABASE_FILE')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO monitored_youtube_channels (user_id, channel_id, is_active) VALUES (?, ?, ?)", (user_id, 'UC-test-channel', True))
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
        cursor.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)", (new_user.id, new_user.email, new_user.password_hash))
        conn.commit()
        conn.close()
        user_id = new_user.id
    assert user_id is not None
    return user_id

# --- Test principale (Radicalmente Semplificato) ---
def test_scheduler_calls_processor_with_correct_data(app, monkeypatch):
    """
    Verifica che il job dello scheduler chiami la funzione di processing
    con i dati corretti presi dal database.
    """
    # 1. ARRANGE
    user_id = register_test_user_for_scheduler(app, monkeypatch, email="integration@test.com")
    setup_test_monitoring_data(app, user_id)
    
    # Definiamo i path delle funzioni che vogliamo "spiare"
    path_youtube_processor = 'app.scheduler_jobs._process_youtube_channel_core'
    path_rss_processor = 'app.scheduler_jobs._process_rss_feed_core'

    # Creiamo le nostre "spie"
    with patch(path_youtube_processor, return_value={'success': True}) as mock_youtube_core, \
         patch(path_rss_processor, return_value=True) as mock_rss_core:
        
        from app.scheduler_jobs import check_monitored_sources_job
        
        # 2. ACT
        with app.app_context():
            check_monitored_sources_job()

        # 3. ASSERT
        # Verifichiamo semplicemente che le nostre spie siano state chiamate
        # con gli argomenti che ci aspettavamo.
        mock_youtube_core.assert_called_once()
        
        # Estraiamo gli argomenti con cui è stata chiamata la spia
        args, kwargs = mock_youtube_core.call_args
        
        # Verifichiamo gli argomenti posizionali
        assert args[0] == 'UC-test-channel' # Il channel_id dal DB
        assert args[1] == user_id           # L'user_id corretto
        assert isinstance(args[2], dict)    # Il dizionario di configurazione