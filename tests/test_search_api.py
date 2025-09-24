import pytest
import json
from unittest.mock import patch, MagicMock, ANY
from flask import url_for
import sqlite3

# Helper per registrare e loggare un utente.
def register_and_login_test_user(client, app, monkeypatch, email="testsearch@example.com", password="password"):
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'email': email, 'password': password, 'confirm_password': password})
    client.post(url_for('login'), data={'email': email, 'password': password})

def test_search_api_success(client, app, monkeypatch):
    """
    Testa l'API /api/search/ con successo, mockando tutte le dipendenze esterne.
    """
    # 1. ARRANGE (Setup)
    
    # Registra un utente per la sessione, necessario per le chiamate interne autenticate
    register_and_login_test_user(client, app, monkeypatch)
    monkeypatch.setitem(app.config, 'COHERE_API_KEY', 'fake_cohere_key_for_testing')

    # Dati finti che i nostri "attori" (mock) restituiranno
    mock_query_embedding = [0.1] * 768
    mock_chroma_results = {
        'ids': [['video_chunk_1']],
        'documents': [['Testo del chunk recuperato da ChromaDB.']],
        'metadatas': [[{'video_id': 'vid1', 'video_title': 'Titolo Test', 'source_type': 'video'}]],
        'distances': [[0.1]]
    }
    # Mock per Cohere: deve restituire una lista di oggetti con attributi 'index' e 'relevance_score'
    mock_rerank_hit = MagicMock()
    mock_rerank_hit.index = 0
    mock_rerank_hit.relevance_score = 0.99
    mock_rerank_results = MagicMock()
    mock_rerank_results.results = [mock_rerank_hit]

    mock_llm_answer = "Questa è la risposta finale generata dall'LLM."

    # Definiamo i path di TUTTE le funzioni e classi che dobbiamo "ingannare"
    path_generate_embeddings = 'app.api.routes.search.generate_embeddings'
    path_cohere_client = 'app.api.routes.search.cohere.Client'
    path_genai_model = 'app.api.routes.search.genai.GenerativeModel'
    
    # Mock per ChromaDB
    mock_chroma_collection = MagicMock()
    mock_chroma_collection.query.return_value = mock_chroma_results
    mock_chroma_client = MagicMock()
    mock_chroma_client.get_collection.return_value = mock_chroma_collection

    # Usiamo un unico blocco 'with' per gestire tutti i nostri "attori"
    with patch(path_generate_embeddings, return_value=[mock_query_embedding]) as mock_embed, \
         patch(path_cohere_client) as MockCohereClient, \
         patch(path_genai_model) as MockGenerativeModel, \
         patch.dict(app.config, {'CHROMA_CLIENT': mock_chroma_client}):
        
        # Configuriamo gli "attori" che sono classi
        mock_cohere_instance = MockCohereClient.return_value
        mock_cohere_instance.rerank.return_value = mock_rerank_results
        
        mock_llm_instance = MockGenerativeModel.return_value
        mock_llm_instance.generate_content.return_value = MagicMock(text=mock_llm_answer)

        # 2. ACT (Esegui l'azione)
        payload = {"query": "Domanda di test"}
        response = client.post(url_for('search.handle_search_request'), json=payload)

        # 3. ASSERT (Verifica i risultati)
        assert response.status_code == 200
        data = response.json
        assert data['success'] is True
        assert data['answer'] == mock_llm_answer
        assert len(data['retrieved_results']) >= 1
        assert data['retrieved_results'][0]['metadata']['video_id'] == 'vid1'

        # Verifica che i nostri "attori" siano stati chiamati come previsto
        mock_embed.assert_called_once()
        MockCohereClient.assert_called_once()
        mock_cohere_instance.rerank.assert_called_once()
        MockGenerativeModel.assert_called_once()
        mock_llm_instance.generate_content.assert_called_once()
        mock_chroma_client.get_collection.assert_called() # Chiamato più volte, una per tipo di contenuto