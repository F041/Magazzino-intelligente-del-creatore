import pytest
import os
import shutil
import io
import sqlite3 # Importa sqlite3 qui se lo usi direttamente nei test
from unittest.mock import patch, MagicMock
from flask import url_for
import logging

logger = logging.getLogger(__name__)

# Helper per il login (lo manteniamo)
def login_test_user_for_docs(client, app, email="docupload@example.com", password="password"):
    client.post(url_for('register'), data={
        'email': email, 'password': password, 'confirm_password': password
    }, follow_redirects=True)
    # Recupera l'user_id dopo la registrazione
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
        conn.close()

    # Esegui il login
    client.post(url_for('login'), data={'email': email, 'password': password}, follow_redirects=True)
    return user_id # Restituisci l'user_id per usarlo nei test SAAS

# Test di upload esistente (lo manteniamo)
def test_document_upload_success(client, app):
    # ... (codice del test di upload come prima) ...
    login_test_user_for_docs(client, app) # Passa app qui
    file_content = b"Contenuto del mio file di test per l'upload."
    file_data = {'documents': (io.BytesIO(file_content), 'test_upload.txt')}

    def mock_index_document_side_effect(doc_id_param, conn_param, user_id_param=None):
        logger.info(f"MOCK _index_document (tests/test_document_api.py): Aggiorno stato per doc_id {doc_id_param} a 'completed'")
        cursor = conn_param.cursor()
        cursor.execute("UPDATE documents SET processing_status = ? WHERE doc_id = ?",
                       ('completed', doc_id_param))
        return 'completed'

    with patch('app.api.routes.documents._index_document', side_effect=mock_index_document_side_effect) as mock_index_doc_func:
        response = client.post(
            url_for('documents.upload_documents'),
            data=file_data,
            content_type='multipart/form-data'
        )

        assert response.status_code == 200
        data = response.json
        assert data['success'] is True
        assert len(data.get('files_indexed_ok', [])) == 1
        assert data['files_indexed_ok'][0]['original_filename'] == 'test_upload.txt'
        mock_index_doc_func.assert_called_once()
        doc_id_created = data['files_indexed_ok'][0].get('doc_id')
        assert doc_id_created is not None

        with app.app_context():
            conn_db_check = sqlite3.connect(app.config['DATABASE_FILE'])
            cursor_db_check = conn_db_check.cursor()
            cursor_db_check.execute("SELECT original_filename, stored_filename, processing_status FROM documents WHERE doc_id = ?", (doc_id_created,))
            db_doc_record = cursor_db_check.fetchone()
            conn_db_check.close()
            assert db_doc_record is not None
            assert db_doc_record[0] == 'test_upload.txt'
            assert db_doc_record[1].startswith(doc_id_created)
            assert db_doc_record[1].endswith('.md')
            assert db_doc_record[2] == 'completed'
            stored_md_filename = db_doc_record[1]
            expected_md_filepath = os.path.join(app.config['UPLOAD_FOLDER_PATH'], stored_md_filename)
            assert os.path.exists(expected_md_filepath)
            with open(expected_md_filepath, 'r', encoding='utf-8') as f_md:
                saved_content = f_md.read()
            expected_saved_content = file_content.decode('utf-8').strip()
            normalized_saved_content = '\n'.join(line.strip() for line in saved_content.splitlines() if line.strip())
            assert normalized_saved_content == expected_saved_content


def create_test_document_direct_db(app, user_id_for_doc, original_filename="doc_to_delete.txt", content="contenuto da eliminare"):
    """
    Helper per creare un record di documento e il suo file .md direttamente
    per i test di eliminazione. Restituisce il doc_id.
    """
    doc_id = None
    filepath_md = None
    with app.app_context():
        doc_id = str(__import__('uuid').uuid4()) # Genera un uuid
        stored_filename_md = f"{doc_id}.md"
        upload_folder = app.config['UPLOAD_FOLDER_PATH']
        filepath_md = os.path.join(upload_folder, stored_filename_md)

        # Crea il file .md fittizio
        os.makedirs(upload_folder, exist_ok=True)
        with open(filepath_md, 'w', encoding='utf-8') as f:
            f.write(content)

        filesize = os.path.getsize(filepath_md)

        # Inserisci nel DB
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO documents (doc_id, original_filename, stored_filename, filepath, filesize, mimetype, user_id, processing_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (doc_id, original_filename, stored_filename_md, filepath_md, filesize, "text/plain", user_id_for_doc, "completed"))
        conn.commit()
        conn.close()
        logger.info(f"HELPER_DB: Creato documento fittizio ID {doc_id} per utente {user_id_for_doc} in {filepath_md}")
    return doc_id, filepath_md


def test_document_delete_success(client, app):
    email_for_delete_test = "delete_doc_user@example.com"
    user_id_for_doc = login_test_user_for_docs(client, app, email=email_for_delete_test)
    doc_id_to_delete, md_filepath_to_delete = create_test_document_direct_db(app, user_id_for_doc)
    assert os.path.exists(md_filepath_to_delete)

    mock_chroma_doc_collection_instance = MagicMock()
    mock_chroma_doc_collection_instance.get.return_value = {'ids': [f'{doc_id_to_delete}_chunk_0']}
    mock_chroma_doc_collection_instance.delete.return_value = None

    # Salva un riferimento al metodo .get originale PRIMA di patchare
    original_config_get = app.config.get

    def config_get_side_effect(key, default=None):
        current_app_mode = original_config_get('APP_MODE') # Usa il .get originale per APP_MODE

        if key == 'CHROMA_DOC_COLLECTION' and current_app_mode == 'single':
            logger.debug(f"MOCK side_effect: Ritorno mock_chroma_doc_collection_instance per CHROMA_DOC_COLLECTION")
            return mock_chroma_doc_collection_instance

        if key == 'CHROMA_CLIENT' and current_app_mode == 'saas':
            logger.debug(f"MOCK side_effect: Ritorno mock_client per CHROMA_CLIENT")
            mock_client_saas = MagicMock()
            mock_client_saas.get_collection.return_value = mock_chroma_doc_collection_instance
            return mock_client_saas

        # Per tutte le altre chiavi, usa il metodo .get originale per evitare ricorsione
        # logger.debug(f"MOCK side_effect: Chiamo original_config_get per chiave '{key}'")
        return original_config_get(key, default)

    patch_path = 'app.api.routes.documents.current_app.config.get'

    with patch(patch_path, side_effect=config_get_side_effect) as mock_config_get_method:
        delete_url = url_for('documents.delete_document', doc_id=doc_id_to_delete)
        response = client.delete(delete_url)

    # ... (asserzioni come prima) ...
    assert response.status_code == 200
    data = response.json
    assert data['success'] is True
    assert "eliminato con successo" in data['message'].lower()
    assert data['chroma_delete_success'] is True

    mock_chroma_doc_collection_instance.get.assert_called_once_with(where={"doc_id": doc_id_to_delete}, include=[])
    mock_chroma_doc_collection_instance.delete.assert_called_once_with(ids=[f'{doc_id_to_delete}_chunk_0'])

    assert not os.path.exists(md_filepath_to_delete)
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT doc_id FROM documents WHERE doc_id = ?", (doc_id_to_delete,))
        db_doc_after_delete = cursor.fetchone()
        conn.close()
        assert db_doc_after_delete is None

def test_document_delete_not_found(client, app):
    """Testa l'eliminazione di un documento che non esiste."""
    login_test_user_for_docs(client, app, email="delnotfound@example.com") # Passa app

    delete_url = url_for('documents.delete_document', doc_id="non-existent-uuid")
    response = client.delete(delete_url)

    assert response.status_code == 404
    data = response.json
    assert data['success'] is False
    assert data['error_code'] == 'DOCUMENT_NOT_FOUND'

# Aggiungere test per SAAS mode:
# - test_document_delete_saas_unauthorized: utente prova a cancellare doc di un altro utente.
#   Questo richiederebbe di impostare TestConfig.APP_MODE = 'saas' e creare due utenti
#   e un documento per il primo utente. Poi il secondo utente tenta di cancellarlo.
#   L'API dovrebbe restituire 404 (perché non lo trova *per quell'utente*).
