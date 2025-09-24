import pytest
from unittest.mock import patch, MagicMock
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
    Testa che il job dello scheduler recuperi correttamente i dati dal DB
    e chiami le funzioni di processing per YouTube e RSS.
    """
    # 1. ARRANGE (Prepara l'ambiente)
    
    # Crea un utente di test e dei dati da monitorare nel database
    user_id = register_test_user_for_scheduler(app, monkeypatch)
    setup_test_monitoring_data(app, user_id)

    # Definiamo i path delle funzioni che vogliamo "spiare" (mockare)
    # Questi sono i nuovi path corretti dopo il nostro refactoring
    path_youtube_processor = 'app.scheduler_jobs._process_youtube_channel_core'
    path_rss_processor = 'app.scheduler_jobs._process_rss_feed_core'

    # Usiamo 'patch' per sostituire temporaneamente le vere funzioni con delle "spie"
    with patch(path_youtube_processor, return_value={'success': True}) as mock_youtube_func, \
         patch(path_rss_processor, return_value=True) as mock_rss_func:
        
        # Importiamo la funzione del job che vogliamo testare
        from app.scheduler_jobs import check_monitored_sources_job
        
        # 2. ACT (Esegui l'azione)
        
        # Eseguiamo la funzione del job all'interno del contesto dell'app,
        # così può accedere a `current_app.config` etc.
        with app.app_context():
            check_monitored_sources_job()

        # 3. ASSERT (Verifica i risultati)
        
        # Verifichiamo che la nostra spia per YouTube sia stata chiamata una volta
        mock_youtube_func.assert_called_once()
        # Verifichiamo che la nostra spia per RSS sia stata chiamata una volta
        mock_rss_func.assert_called_once()
        
        # Possiamo anche controllare con quali "ingredienti" sono state chiamate
        youtube_call_args = mock_youtube_func.call_args[0]
        assert youtube_call_args[0] == 'UC-test-channel' # L'ID del canale
        assert youtube_call_args[1] == user_id           # L'ID dell'utente

        rss_call_args = mock_rss_func.call_args[0]
        assert rss_call_args[0] == 'http://test.com/feed' # L'URL del feed
        assert rss_call_args[1] == user_id                # L'ID dell'utente