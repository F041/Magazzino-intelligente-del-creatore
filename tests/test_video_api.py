import pytest
import time
import sqlite3
import os # Per os.path.join
from unittest.mock import patch, MagicMock, ANY 
from flask import url_for, current_app # current_app per accedere a config dentro il test
from app.services.embedding.gemini_embedding import TASK_TYPE_DOCUMENT

def login_test_user_for_videos(client, app, email="videouser@example.com", password="password"):
    client.post(url_for('register'), data={
        'email': email, 'password': password, 'confirm_password': password
    }, follow_redirects=True)
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row: user_id = user_row[0]
        conn.close()
    client.post(url_for('login'), data={'email': email, 'password': password}, follow_redirects=True)
    return user_id

# Funzione helper per creare un video fittizio nel DB
def create_test_video_in_db(app, user_id_for_video, video_id="testvid001", title="Test Video", initial_status="completed"):
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO videos 
            (video_id, title, url, channel_id, published_at, description, transcript, transcript_language, captions_type, user_id, processing_status, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (video_id, title, f"https://youtube.com/watch?v={video_id}", "testchannel", "2023-01-01T00:00:00Z", 
              "Descrizione test", "Trascrizione iniziale fittizia." if initial_status == "completed" else None, 
              "it" if initial_status == "completed" else None, "manual" if initial_status == "completed" else None, 
              user_id_for_video, initial_status))
        conn.commit()
        conn.close()
    return video_id


def test_reprocess_single_video_success(client, app):
    """
    Testa POST /api/videos/<video_id>/reprocess con successo.
    Mocka TranscriptService, get_gemini_embeddings, e ChromaDB.
    """
    # 1. Setup utente e video iniziale
    email_reprocess = "reprocessuser@example.com"
    # Assicurati che APP_MODE in TestConfig sia 'single' per semplificare user_id qui
    user_id_for_video = login_test_user_for_videos(client, app, email=email_reprocess)
    if app.config['APP_MODE'] == 'saas' and user_id_for_video is None:
        pytest.fail("User ID non creato correttamente per test SAAS")
    
    video_id_to_reprocess = create_test_video_in_db(app, 
                                                   user_id_for_video if app.config['APP_MODE'] == 'saas' else None, 
                                                   video_id="vidreproc01", 
                                                   initial_status="failed_transcript") # Inizia con uno stato che necessita riprocessamento

    # 2. Definisci i valori di ritorno dei mock
    mock_transcript_text = "Nuova trascrizione mockata per il riprocessamento."
    mock_transcript_result = {'text': mock_transcript_text, 'language': 'en', 'type': 'auto'}
    mock_embeddings = [[0.5] * 768, [0.6] * 768] # Due chunk fittizi
    mock_chunks = ["Chunk 1 della nuova trascrizione.", "Chunk 2 della nuova trascrizione."]

    # 3. Patch delle dipendenze esterne nella route videos.py
    # I path devono puntare a dove gli oggetti sono USATI/IMPORTATI in app.api.routes.videos
    path_transcript_service = 'app.api.routes.videos.TranscriptService.get_transcript'
    path_split_chunks = 'app.api.routes.videos.split_text_into_chunks' # Usato da reprocess_single_video
    path_get_embeddings = 'app.api.routes.videos.get_gemini_embeddings'
    
    # Mock per ChromaDB (simile a test_document_delete_success)
    mock_chroma_video_collection_instance = MagicMock()
    mock_chroma_video_collection_instance.get.return_value = {'ids': [f'{video_id_to_reprocess}_chunk_old_0']} # Simula vecchi chunk
    mock_chroma_video_collection_instance.delete.return_value = None
    mock_chroma_video_collection_instance.upsert.return_value = None

    # Funzione side_effect per il config.get (come in test_document_api)
    original_config_get = app.config.get
    def config_get_side_effect_video(key, default=None):
        current_app_mode = original_config_get('APP_MODE')
        if key == 'CHROMA_VIDEO_COLLECTION' and current_app_mode == 'single':
            return mock_chroma_video_collection_instance
        if key == 'CHROMA_CLIENT' and current_app_mode == 'saas':
            mock_client_saas = MagicMock()
            mock_client_saas.get_or_create_collection.return_value = mock_chroma_video_collection_instance # Usa get_or_create
            return mock_client_saas
        return original_config_get(key, default)

    patch_path_config_get = 'app.api.routes.videos.current_app.config.get'

    with patch(path_transcript_service, return_value=mock_transcript_result) as mock_get_transcript, \
         patch(path_split_chunks, return_value=mock_chunks) as mock_split, \
         patch(path_get_embeddings, return_value=mock_embeddings) as mock_embed, \
         patch(patch_path_config_get, side_effect=config_get_side_effect_video) as mock_config_access:

        # 4. Chiama l'API di riprocessamento
        reprocess_url = url_for('videos.reprocess_single_video', video_id=video_id_to_reprocess)
        response = client.post(reprocess_url)

        # 5. Verifica la risposta API
        assert response.status_code == 200
        data = response.json
        assert data['success'] is True
        assert data['new_status'] == 'completed'
        assert "riprocessamento" in data['message'].lower()
        assert "completed" in data['message'].lower() # Cerca 'completed'

        # 6. Verifica che i mock siano stati chiamati
        mock_get_transcript.assert_called_once_with(video_id_to_reprocess)
        mock_split.assert_called_once_with(mock_transcript_text, chunk_size=app.config['DEFAULT_CHUNK_SIZE_WORDS'], chunk_overlap=app.config['DEFAULT_CHUNK_OVERLAP_WORDS'])
        mock_embed.assert_called_once_with(mock_chunks, api_key=app.config['GOOGLE_API_KEY'], model_name=app.config['GEMINI_EMBEDDING_MODEL'], task_type=TASK_TYPE_DOCUMENT)
        
        # Verifica chiamate a ChromaDB (delete e upsert)
        mock_chroma_video_collection_instance.get.assert_called_once_with(where={"video_id": video_id_to_reprocess}, include=[])
        mock_chroma_video_collection_instance.delete.assert_called_once_with(ids=[f'{video_id_to_reprocess}_chunk_old_0'])
        
        # Verifica argomenti di upsert (più complesso, ma importante)
        # Costruisci gli ID e i metadati attesi per l'upsert
        expected_upsert_ids = [f"{video_id_to_reprocess}_chunk_{i}" for i in range(len(mock_chunks))]
        # Per i metadati, dobbiamo recuperare i dettagli del video dal DB perché sono usati nella route
        video_meta_for_assert = {}
        with app.app_context(): # Per accedere a DB e config
            conn = sqlite3.connect(app.config['DATABASE_FILE'])
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT title, channel_id, published_at FROM videos WHERE video_id = ?", (video_id_to_reprocess,))
            meta_row = cursor.fetchone()
            if meta_row: video_meta_for_assert = dict(meta_row)
            conn.close()

        expected_metadatas_upsert = [{
            'video_id': video_id_to_reprocess, 
            'channel_id': video_meta_for_assert.get('channel_id'), 
            'video_title': video_meta_for_assert.get('title'),
            'published_at': str(video_meta_for_assert.get('published_at')), 
            'chunk_index': i, 
            'language': mock_transcript_result['language'],
            'caption_type': mock_transcript_result['type'],
            **( {"user_id": user_id_for_video} if app.config['APP_MODE'] == 'saas' else {} )
            } for i in range(len(mock_chunks))]

        mock_chroma_video_collection_instance.upsert.assert_called_once_with(
            ids=expected_upsert_ids,
            embeddings=mock_embeddings,
            metadatas=expected_metadatas_upsert,
            documents=mock_chunks
        )

        # 7. Verifica lo stato finale nel DB SQLite
        with app.app_context():
            conn = sqlite3.connect(app.config['DATABASE_FILE'])
            cursor = conn.cursor()
            cursor.execute("SELECT processing_status, transcript, transcript_language, captions_type FROM videos WHERE video_id = ?", (video_id_to_reprocess,))
            db_video_after = cursor.fetchone()
            conn.close()
            assert db_video_after is not None
            assert db_video_after[0] == 'completed'
            assert db_video_after[1] == mock_transcript_text
            assert db_video_after[2] == mock_transcript_result['language']
            assert db_video_after[3] == mock_transcript_result['type']

