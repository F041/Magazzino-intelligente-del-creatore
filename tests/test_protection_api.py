import pytest
import os
import sqlite3
import zipfile
import io
import shutil
import tempfile
# <-- CORREZIONE 3: Importiamo patch e MagicMock -->
from unittest.mock import patch, MagicMock
from flask import url_for
# Importiamo la funzione che vogliamo testare direttamente
from app.main import _handle_startup_restore

# --- Funzione Helper (CORRETTA) ---
def setup_test_user_and_data(client, app, monkeypatch, email, video_id, video_title):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'email': email, 'password': 'password', 'confirm_password': 'password'})
    client.post(url_for('login'), data={'email': email, 'password': 'password'})
    
    user_id = None
    with app.app_context():
        db_path = app.config['DATABASE_FILE']
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        user_id = user_row[0]
        
        cursor.execute("DELETE FROM videos WHERE user_id = ?", (user_id,))
        # <-- CORREZIONE 1: Aggiungiamo tutti i campi NOT NULL -->
        cursor.execute(
            """INSERT INTO videos 
               (video_id, title, user_id, channel_id, published_at, url, processing_status) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (video_id, video_title, user_id, "chan1", "2025-01-01T00:00:00Z", "http://fake.url", "completed")
        )
        conn.commit()

        # Inserisci un chunk di test nel database vettoriale ChromaDB
        chroma_client = app.config['CHROMA_CLIENT']
        if chroma_client: # Aggiunto controllo per sicurezza
            collection_name = f"video_transcripts_{user_id}"
            collection = chroma_client.get_or_create_collection(name=collection_name)
            collection.add(
                ids=[f"{video_id}_chunk_0"],
                documents=["Contenuto del chunk."],
                metadatas=[{"video_id": video_id}]
            )
        conn.close()
    
    assert user_id is not None
    return user_id


# --- Test di Download ---
def test_full_backup_download(client, app, monkeypatch):
    setup_test_user_and_data(client, app, monkeypatch, "backup_user@test.com", "vid_bk", "Video Backup")
    response = client.get(url_for('protection.download_full_backup'))
    assert response.status_code == 200
    assert response.mimetype == 'application/zip'
    zip_file = zipfile.ZipFile(io.BytesIO(response.data))
    assert 'test_magazzino.db' in zip_file.namelist()
    assert any(name.startswith('test_chroma_db/') for name in zip_file.namelist())

# --- Test di Upload ---
def test_full_restore_upload(client, app, monkeypatch):
    # <-- CORREZIONE 2: Usiamo un'email unica e l'helper corretto -->
    setup_test_user_and_data(client, app, monkeypatch, "upload_user@test.com", "vid_up", "Video Upload")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        zf.writestr("test.txt", "content")
    zip_buffer.seek(0)

    response = client.post(
        url_for('protection.restore_full_backup'),
        data={'backup_file': (zip_buffer, 'backup.zip')},
        content_type='multipart/form-data'
    )

    assert response.status_code == 200
    with app.app_context():
        data_dir = os.path.dirname(app.config.get('DATABASE_FILE'))
        assert os.path.exists(os.path.join(data_dir, 'pending_restore.zip'))

# --- TEST DEL RIPRISTINO ---
def test_live_restore_simulation(app):
    with tempfile.TemporaryDirectory() as temp_dir:
        # Crea i dati del backup (il DB "nuovo")
        source_db_path = os.path.join(temp_dir, 'source.db')
        conn = sqlite3.connect(source_db_path)
        cursor = conn.cursor()
        # Aggiungiamo tutte le colonne necessarie alla tabella finta
        cursor.execute("CREATE TABLE videos (video_id TEXT, title TEXT, user_id TEXT, channel_id TEXT, published_at TEXT, url TEXT, processing_status TEXT)")
        cursor.execute("INSERT INTO videos (video_id, title) VALUES (?, ?)", ("vid_nuovo", "VIDEO RIPRISTINATO"))
        conn.commit()
        conn.close()

        # Crea la cartella dati di destinazione
        target_data_dir = os.path.join(temp_dir, 'data')
        os.makedirs(os.path.join(target_data_dir, 'chroma_db'), exist_ok=True)
        
        # Metti il backup in attesa nella destinazione
        pending_zip_path = os.path.join(target_data_dir, 'pending_restore.zip')
        with zipfile.ZipFile(pending_zip_path, 'w') as zf:
            # Usiamo il nome del file come definito in TestConfig
            zf.write(source_db_path, arcname='test_magazzino.db') 

        # Crea un finto DB "vecchio" che deve essere cancellato
        old_db_path = os.path.join(target_data_dir, 'test_magazzino.db')
        conn = sqlite3.connect(old_db_path)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE videos (video_id TEXT, title TEXT, user_id TEXT, channel_id TEXT, published_at TEXT, url TEXT, processing_status TEXT)")
        cursor.execute("INSERT INTO videos (video_id, title) VALUES (?, ?)", ("vid_vecchio", "VIDEO DA CANCELLARE"))
        conn.commit()
        conn.close()

        # Prepara una finta configurazione che punti a questa cartella
        mock_config = {
            'DATABASE_FILE': old_db_path,
            'CHROMA_PERSIST_PATH': os.path.join(target_data_dir, 'chroma_db')
        }

        # ACT: Chiama la funzione di ripristino, simulando l'avvio dell'app
        with patch('app.main.sys.exit') as mock_exit:
            _handle_startup_restore(mock_config)
            mock_exit.assert_called_once_with(0)

        # ASSERT: Controlla i risultati nella cartella di destinazione
        assert not os.path.exists(pending_zip_path)
        conn = sqlite3.connect(old_db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT title FROM videos WHERE video_id = ?", ("vid_nuovo",))
        restored_video = cursor.fetchone()
        assert restored_video is not None
        assert restored_video[0] == "VIDEO RIPRISTINATO"

        cursor.execute("SELECT * FROM videos WHERE video_id = ?", ("vid_vecchio",))
        assert cursor.fetchone() is None
        
        conn.close()