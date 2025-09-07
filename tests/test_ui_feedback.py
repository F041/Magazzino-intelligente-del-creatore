# FILE: tests/test_ui_feedback.py (Versione Corretta)

import pytest
from unittest.mock import patch, MagicMock
from flask import url_for
import sqlite3

# --- Le funzioni Helper rimangono identiche ---

def login_test_user_for_feedback(client, app, email="uitest@example.com", password="password"):
    client.post(url_for('register'), data={
        'email': email, 'password': password, 'confirm_password': password
    })
    client.post(url_for('login'), data={'email': email, 'password': password})
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_id = cursor.fetchone()[0]
        conn.close()
        return user_id

def create_test_video_for_feedback(app, user_id, video_id="test_vid_ui_01"):
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO videos (video_id, title, url, channel_id, published_at, user_id, processing_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (video_id, "Test Video for UI", "http://fake.url", "fake_channel", "2023-01-01T00:00:00Z", user_id, "completed"))
        conn.commit()
        conn.close()
    return video_id


# --- Il Test Vero e Proprio (Modificato) ---

def test_reprocess_failure_shows_banner_message(client, app):
    """
    TEST: Verifica che, quando il riprocessamento di un video fallisce,
    il server restituisca un JSON di errore che il frontend userà per mostrare il banner.
    """
    
    # 1. ARRANGE (preparazione)
    user_id = login_test_user_for_feedback(client, app)
    video_id = create_test_video_for_feedback(app, user_id)
    
    messaggio_di_errore_simulato = "Trascrizione non trovata a causa di un'eclissi solare."
    risultato_di_fallimento_simulato = {'error': 'TRANSCRIPT_FAILED', 'message': messaggio_di_errore_simulato}

    # Definiamo i percorsi dei "bersagli" da colpire con i nostri dardi soporiferi (patch)
    path_youtube_client = 'app.api.routes.videos.YouTubeClient'
    path_transcript_service = 'app.api.routes.videos.TranscriptService.get_transcript'
    
    # Ora usiamo un blocco "with" multiplo per gestire entrambi i patch
    with patch(path_youtube_client, MagicMock()) as mock_yt_client_class, \
         patch(path_transcript_service, return_value=risultato_di_fallimento_simulato) as mock_transcript_service:
        
        # 2. ACT (azione)
        response = client.post(url_for('videos.reprocess_single_video', video_id=video_id))

        # 3. ASSERT (verifica)
        assert response.status_code == 200
        data = response.json
        assert data['success'] is False
        
        # ORA IL TEST PASSERA', perché l'errore delle credenziali è stato "addormentato"
        # e la funzione ha potuto raggiungere il punto in cui il nostro finto servizio
        # di trascrizioni ha generato l'errore che ci aspettavamo.
        assert data['error_code'] == 'TRANSCRIPT_FAILED' 
        assert messaggio_di_errore_simulato in data['message']
        
        # Verifichiamo che il nostro finto servizio sia stato chiamato.
        mock_transcript_service.assert_called_once()