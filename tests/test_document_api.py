import pytest
import os
import io
import sqlite3
from unittest.mock import patch, MagicMock
from flask import url_for
import logging

logger = logging.getLogger(__name__)

# --- Funzioni Helper (Complete e invariate) ---

def login_and_get_user_id(client, app, monkeypatch, email):
    """Registra e logga un utente, restituendo il suo ID."""
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
    """Crea un record di documento fittizio direttamente nel DB per i test."""
    doc_id = str(__import__('uuid').uuid4())
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO documents (doc_id, original_filename, content, user_id, processing_status) VALUES (?, ?, ?, ?, ?)",
            (doc_id, original_filename, content, user_id_for_doc, "completed")
        )
        conn.commit()
        conn.close()
    return doc_id, None

# --- Test di Upload Riscritto con Parametrizzazione ---

@pytest.mark.parametrize(
    "use_agentic_chunking_value, should_call_agentic",
    [
        (False, False), # Scenario 1: Chunking classico
        (True, True),   # Scenario 2: Chunking intelligente
    ]
)
def test_document_upload_respects_chunking_configuration(client, app, monkeypatch, use_agentic_chunking_value, should_call_agentic):
    """
    Testa che l'upload del documento chiami il metodo di chunking corretto
    in base alla configurazione dell'app.
    """
    # 1. ARRANGE
    
    # Modifica la configurazione dell'app in memoria per questo test specifico
    monkeypatch.setitem(app.config, 'USE_AGENTIC_CHUNKING', str(use_agentic_chunking_value))
    
    # Prepara l'utente e i dati del file
    login_and_get_user_id(client, app, monkeypatch, email=f"chunk_test_{str(use_agentic_chunking_value)}@example.com")
    file_content = b"Questo e' un testo di prova per il chunking."
    file_data = {'documents': (io.BytesIO(file_content), 'test_upload.txt')}

    # Definisci i path delle funzioni da "spiare"
    path_agentic_chunker = 'app.api.routes.documents.chunk_text_agentically'
    path_classic_chunker = 'app.api.routes.documents.split_text_into_chunks'
    path_generate_embeddings = 'app.api.routes.documents.generate_embeddings' # Dobbiamo simulare anche questo

    # Prepara le "spie" (mocks)
    with patch(path_agentic_chunker, return_value=["chunk intelligente"]) as mock_agentic, \
         patch(path_classic_chunker, return_value=["chunk classico"]) as mock_classic, \
         patch(path_generate_embeddings, return_value=[[0.1]*768]): # Simula una risposta valida per gli embeddings
        
        # 2. ACT
        response = client.post(
            url_for('documents.upload_documents'),
            data=file_data,
            content_type='multipart/form-data'
        )

        # 3. ASSERT
        assert response.status_code == 200
        assert response.json['success'] is True
        
        # Il controllo cruciale
        if should_call_agentic:
            mock_agentic.assert_called_once()
            mock_classic.assert_not_called()
        else:
            mock_agentic.assert_not_called()
            mock_classic.assert_called_once()


# --- Test di Eliminazione (Completi e Invariati) ---

def test_document_delete_success(client, app, monkeypatch):
    """Testa l'eliminazione di un documento con successo."""
    user_id = login_and_get_user_id(client, app, monkeypatch, email="delete_doc_user@example.com")
    doc_id, _ = create_test_document_direct_db(app, user_id)

    mock_chroma_collection = MagicMock()
    mock_chroma_collection.get.return_value = {'ids': [f'{doc_id}_chunk_0']}
    
    mock_chroma_client = MagicMock()
    mock_chroma_client.get_collection.return_value = mock_chroma_collection

    # Usiamo patch.dict per simulare il client Chroma nella configurazione dell'app
    with patch.dict(app.config, {'CHROMA_CLIENT': mock_chroma_client}):
        response = client.delete(url_for('documents.delete_document', doc_id=doc_id))

    assert response.status_code == 200
    data = response.json
    assert data['success'] is True
    
    # Verifichiamo che i metodi di Chroma siano stati chiamati correttamente
    mock_chroma_collection.get.assert_called_once_with(where={"doc_id": doc_id}, include=[])
    mock_chroma_collection.delete.assert_called_once()
    
def test_document_delete_not_found(client, app, monkeypatch):
    """Testa il tentativo di eliminare un documento che non esiste."""
    login_and_get_user_id(client, app, monkeypatch, email="delnotfound@example.com")
    response = client.delete(url_for('documents.delete_document', doc_id="uuid-che-non-esiste"))
    
    assert response.status_code == 404
    data = response.json
    assert data['success'] is False
    assert data['error_code'] == 'DOCUMENT_NOT_FOUND'