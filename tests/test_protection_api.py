import pytest
import os
import sqlite3
import zipfile
import io
import shutil
import tempfile
from unittest.mock import MagicMock
from flask import url_for

# --- Funzione Helper per creare un utente e dei dati di test ---
# Ci serve per avere qualcosa da backuppare e ripristinare.
def setup_test_user_and_data(client, app, monkeypatch, email="backup_user@example.com"):

    monkeypatch.setenv("ALLOWED_EMAILS", email)
    # Registra e logga l'utente
    client.post(url_for('register'), data={'email': email, 'password': 'password', 'confirm_password': 'password'})
    client.post(url_for('login'), data={'email': email, 'password': 'password'})
    
    user_id = None
    with app.app_context():
        # Recupera l'ID dell'utente appena creato
        db_path = app.config['DATABASE_FILE']
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        user_id = user_row[0]
        
        # Inserisci un video di test nel database SQLite
        cursor.execute(
            "INSERT INTO videos (video_id, title, user_id, channel_id, published_at, url, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("vid_backup_test", "Video Originale", user_id, "chan1", "2025-01-01T00:00:00Z", "http://fake.url", "completed")
        )
        conn.commit()

        # Inserisci un chunk di test nel database vettoriale ChromaDB
        chroma_client = app.config['CHROMA_CLIENT']
        collection_name = f"video_transcripts_{user_id}"
        collection = chroma_client.get_or_create_collection(name=collection_name)
        collection.add(
            ids=["vid_backup_test_chunk_0"],
            documents=["Contenuto del chunk originale."],
            metadatas=[{"video_id": "vid_backup_test"}]
        )
        conn.close()
    
    assert user_id is not None
    return user_id


# --- Test #1: Verifica il download del backup completo ---
def test_full_backup_download(client, app, monkeypatch):
    """Verifica che l'endpoint di backup completo restituisca un file ZIP valido."""
    # ARRANGE: Crea un utente e dei dati da backuppare
    setup_test_user_and_data(client, app, monkeypatch)

    # ACT: Chiama l'endpoint di download
    response = client.get(url_for('protection.download_full_backup'))

    # ASSERT: Controlla che la risposta sia corretta
    assert response.status_code == 200
    assert response.mimetype == 'application/zip'
    
    # Controlliamo anche che il file ZIP sia valido e contenga i nostri file
    zip_file = zipfile.ZipFile(io.BytesIO(response.data))
    filenames_in_zip = zip_file.namelist()
    
    assert 'test_magazzino.db' in filenames_in_zip
    # Verifichiamo che ci sia almeno un file del database vettoriale
    assert any(name.startswith('test_chroma_db/') for name in filenames_in_zip)


# --- Test #2: Verifica il caricamento del file di ripristino ---
def test_full_restore_upload(client, app, monkeypatch):
    """Verifica che il caricamento di un backup salvi il file 'pending_restore.zip'."""
    setup_test_user_and_data(client, app, monkeypatch, email="restore_upload@example.com")

    # Creiamo un finto file ZIP in memoria
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        zf.writestr("test.txt", "contenuto del backup")
    zip_buffer.seek(0)

    # ACT: Simula il caricamento del file
    response = client.post(
        url_for('protection.restore_full_backup'),
        data={'backup_file': (zip_buffer, 'backup.zip')},
        content_type='multipart/form-data'
    )

    # ASSERT: Controlla la risposta e l'esistenza del file
    assert response.status_code == 202
    assert response.json['success'] is True
    
    with app.app_context():
        data_dir = os.path.dirname(app.config.get('DATABASE_FILE'))
        pending_file_path = os.path.join(data_dir, 'pending_restore.zip')
        assert os.path.exists(pending_file_path)


# --- Test #3: Verifica la logica di ripristino all'avvio (il test più importante) ---
def test_startup_restore_logic(app):
    """
    Simula l'intero processo di ripristino:
    1. Crea un ambiente SORGENTE con dati specifici.
    2. Crea un backup di questo ambiente.
    3. Crea un ambiente DESTINAZIONE con dati DIVERSI.
    4. Mette il backup in attesa nella destinazione.
    5. Avvia una nuova istanza dell'app sulla destinazione.
    6. Verifica che i dati della destinazione ora corrispondano a quelli della sorgente.
    """
    from app.main import create_app
    from app.config import TestConfig

    # Creiamo due cartelle temporanee isolate
    source_dir = tempfile.mkdtemp(prefix="source_")
    target_dir = tempfile.mkdtemp(prefix="target_")

    source_app = None
    target_app_before = None
    final_app = None

    try:
        # --- 1. SETUP DELL'AMBIENTE SORGENTE (quello da backuppare) ---
        source_config = TestConfig()
        source_config._TEST_BASE_DIR = source_dir
        with open(source_config.CLIENT_SECRETS_PATH, 'w') as f:
            f.write('{}')
        
        source_app = create_app(source_config)
        with source_app.app_context():
            source_user_id = "user-sorgente-123"
            db_path = source_app.config['DATABASE_FILE']
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)", (source_user_id, "source@test.com", "hash"))
            cursor.execute("INSERT INTO videos (video_id, title, user_id, channel_id, published_at, url) VALUES (?, ?, ?, ?, ?, ?)", ("vid_sorgente", "VIDEO ORIGINALE", source_user_id, "chan_s", "2025-01-01", "url_s"))
            conn.commit()
            conn.close()

        # --- 2. CREIAMO MANUALMENTE IL BACKUP DALLA SORGENTE ---
        backup_zip_path = os.path.join(target_dir, 'data_for_tests', 'pending_restore.zip')
        source_data_path = os.path.join(source_dir, "data_for_tests")
        # Assicuriamoci che la cartella di destinazione esista
        os.makedirs(os.path.dirname(backup_zip_path), exist_ok=True)
        shutil.make_archive(os.path.splitext(backup_zip_path)[0], 'zip', source_data_path)

        # --- 3. SETUP DELL'AMBIENTE DESTINAZIONE (quello da sovrascrivere) ---
        target_config = TestConfig()
        target_config._TEST_BASE_DIR = target_dir
        with open(target_config.CLIENT_SECRETS_PATH, 'w') as f:
            f.write('{}')

        target_app_before = create_app(target_config)
        with target_app_before.app_context():
            target_user_id = "user-destinazione-456"
            db_path = target_app_before.config['DATABASE_FILE']
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)", (target_user_id, "target@test.com", "hash"))
            cursor.execute("INSERT INTO videos (video_id, title, user_id, channel_id, published_at, url) VALUES (?, ?, ?, ?, ?, ?)", ("vid_dest", "VIDEO DA SOVRASCRIVERE", target_user_id, "chan_t", "2024-01-01", "url_t"))
            conn.commit()
            conn.close()

        # --- 4. ACT: CREIAMO L'APP PUNTANDO ALLA DESTINAZIONE (dove c'è il backup in attesa) ---
        # Questo è il momento in cui la logica di ripristino che abbiamo scritto in main.py dovrebbe scattare.
        final_app = create_app(target_config)

        # --- 5. ASSERT: VERIFICHIAMO LO STATO DELLA DESTINAZIONE DOPO IL RIAVVIO ---
        with final_app.app_context():
            db_path = final_app.config['DATABASE_FILE']
            # Verifichiamo che il file di ripristino sia stato cancellato
            assert not os.path.exists(backup_zip_path)
            
            # Verifichiamo il contenuto del database ripristinato
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            # Il video da sovrascrivere NON deve più esistere
            cursor.execute("SELECT * FROM videos WHERE video_id = ?", ("vid_dest",))
            assert cursor.fetchone() is None
            
            # Il video originale della sorgente DEVE esistere
            cursor.execute("SELECT title FROM videos WHERE video_id = ?", ("vid_sorgente",))
            restored_video = cursor.fetchone()
            assert restored_video is not None
            assert restored_video[0] == "VIDEO ORIGINALE"
            conn.close()

    finally:
        # --- INIZIO BLOCCO DI PULIZIA AGGIORNATO ---
        # 6. Spegniamo esplicitamente TUTTI i client ChromaDB prima di cancellare
        for app_instance in [source_app, target_app_before, final_app]:
            if app_instance:
                chroma_client = app_instance.config.get('CHROMA_CLIENT')
                if chroma_client and hasattr(chroma_client, '_system') and hasattr(chroma_client._system, 'stop'):
                    try:
                        chroma_client._system.stop()
                    except Exception:
                        pass # Ignora errori durante lo shutdown
        
        # Diamo un istante al sistema operativo
        import time
        time.sleep(0.5)

        # Pulizia finale delle cartelle temporanee
        if os.path.exists(source_dir):
            shutil.rmtree(source_dir, ignore_errors=True)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
        # --- FINE BLOCCO DI PULIZIA AGGIORNATO ---