import pytest
import os
import io
import sqlite3
from unittest.mock import patch, MagicMock
from flask import url_for
import logging

logger = logging.getLogger(__name__)

# Helper aggiornato per usare monkeypatch e restituire l'user_id
def login_and_get_user_id(client, app, monkeypatch, email):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'email': email, 'password': 'password', 'confirm_password': 'password'})
    client.post(url_for('login'), data={'email': email, 'password': 'password'})
    
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
        conn.close()
    assert user_id is not None, f"User ID non trovato per {email}"
    return user_id

def create_test_document_direct_db(app, user_id_for_doc, original_filename="doc_to_delete.txt", content="contenuto"):
    doc_id = str(__import__('uuid').uuid4())
    stored_filename_md = f"{doc_id}.md"
    upload_folder = app.config['UPLOAD_FOLDER_PATH']
    filepath_md = os.path.join(upload_folder, stored_filename_md)
    os.makedirs(upload_folder, exist_ok=True)
    with open(filepath_md, 'w', encoding='utf-8') as f:
        f.write(content)
    filesize = os.path.getsize(filepath_md)
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO documents (doc_id, original_filename, stored_filename, filepath, filesize, user_id, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, original_filename, stored_filename_md, filepath_md, filesize, user_id_for_doc, "completed")
        )
        conn.commit()
        conn.close()
    return doc_id, filepath_md

def test_document_upload_success(client, app, monkeypatch):
    login_and_get_user_id(client, app, monkeypatch, email="docupload@example.com")
    file_content = b"Contenuto del mio file."
    file_data = {'documents': (io.BytesIO(file_content), 'test_upload.txt')}

    # Mock completo per _index_document per evitare chiamate reali
    with patch('app.api.routes.documents._index_document', return_value='completed') as mock_index:
        response = client.post(
            url_for('documents.upload_documents'),
            data=file_data,
            content_type='multipart/form-data'
        )
        assert response.status_code == 200, "La richiesta di upload non ha avuto successo"
        data = response.json
        assert data['success'] is True
        mock_index.assert_called_once() # Verifica che l'indicizzazione sia stata chiamata

def test_document_delete_success(client, app, monkeypatch):
    user_id = login_and_get_user_id(client, app, monkeypatch, email="delete_doc_user@example.com")
    doc_id, md_filepath = create_test_document_direct_db(app, user_id)
    assert os.path.exists(md_filepath)

    # Mock per la COLLEZIONE ChromaDB
    mock_chroma_collection = MagicMock()
    mock_chroma_collection.get.return_value = {'ids': [f'{doc_id}_chunk_0']}
    
    # Mock per il CLIENT ChromaDB
    mock_chroma_client = MagicMock()
    # Configuriamo il CLIENT in modo che il suo metodo .get_collection restituisca la nostra COLLEZIONE mockata
    mock_chroma_client.get_collection.return_value = mock_chroma_collection

    # Usiamo patch.dict per sostituire il VERO client nella config con il nostro FALSO client
    with patch.dict(app.config, {
        'CHROMA_CLIENT': mock_chroma_client,
        'CHROMA_DOC_COLLECTION': mock_chroma_collection # Mock anche per la modalità 'single'
    }):
        
        response = client.delete(url_for('documents.delete_document', doc_id=doc_id))

    assert response.status_code == 200
    data = response.json
    assert data['success'] is True
    assert not os.path.exists(md_filepath)

    # ORA le verifiche funzioneranno, perché stiamo controllando la collezione
    mock_chroma_collection.get.assert_called_once_with(where={"doc_id": doc_id}, include=[])
    mock_chroma_collection.delete.assert_called_once()
    
def test_document_delete_not_found(client, app, monkeypatch):
    login_and_get_user_id(client, app, monkeypatch, email="delnotfound@example.com")
    response = client.delete(url_for('documents.delete_document', doc_id="non-existent-uuid"))
    assert response.status_code == 404
    data = response.json
    assert data['success'] is False
    assert data['error_code'] == 'DOCUMENT_NOT_FOUND'