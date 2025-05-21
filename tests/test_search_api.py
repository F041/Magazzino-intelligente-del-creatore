import pytest
import json
from unittest.mock import patch, MagicMock # Importa strumenti di mocking
from flask import url_for

# Assumiamo che le fixture 'app' e 'client' siano definite in tests/conftest.py
# e che 'app' crei un utente di test o che abbiamo un modo per loggare un utente
# per accedere a endpoint protetti da @require_api_key.

# Helper per registrare e loggare un utente (potrebbe essere in conftest.py o duplicato/importato)
def register_and_login_test_user(client, email="testsearch@example.com", password="password"):
    client.post(url_for('register', _external=False), data={ # _external=False è spesso non necessario con test_client
        'email': email, 'password': password, 'confirm_password': password
    }, follow_redirects=True)

    # Per il login, otteniamo l'API Key per questo utente (modalità SAAS)
    # Questo è un po' più complesso per i test API diretti se non vogliamo simulare il flusso UI
    # Per ora, assumiamo che @require_api_key in 'single' mode (da TestConfig) permetta l'accesso,
    # o che la sessione Flask sia sufficiente se l'utente è loggato via UI.
    # Se TestConfig.APP_MODE = 'saas', avremmo bisogno di un'API Key valida.

    # Semplificazione per ora: assumiamo TestConfig.APP_MODE = 'single' o che
    # @require_api_key permetta accesso se una sessione valida esiste (che il login dovrebbe creare).
    login_response = client.post(url_for('login', _external=False), data={
        'email': email, 'password': password
    }, follow_redirects=True)
    return login_response


def test_search_api_success(client, app):
    """Testa l'API /api/search/ con successo, mockando le dipendenze esterne."""

    # 0. (Opzionale/Necessario se APP_MODE='saas' in TestConfig)
    #    Assicurati che un utente sia loggato o fornisci un'API Key valida.
    #    Per TestConfig con APP_MODE='single', l'autenticazione API potrebbe non essere richiesta.
    #    Se APP_MODE='saas', questo test necessiterà di un setup utente e API Key.
    #    Per semplicità, assumiamo che `TestConfig` sia in `single` mode o che
    #    il login Flask sia sufficiente se `@require_api_key` lo permette per le chiamate interne.
    #    Se usi una chiave API fissa per i test, potresti impostarla qui negli header.

    # Per testare con un utente loggato (se l'API lo permette via sessione)
    if app.config['APP_MODE'] == 'saas':
        # In modalità SAAS, il test più realistico usa una X-API-Key.
        # Dobbiamo: 1. Registrare utente, 2. Loggarlo (per la sessione UI, non strettamente per l'API se usiamo X-API-Key),
        # 3. Generare una API Key per quell'utente programmaticamente o con un endpoint di test, 4. Usare quella chiave.
        # Questo è più complesso.
        #
        # Alternativa più semplice per ora se TestConfig.APP_MODE è 'single':
        # Non serve autenticazione specifica per l'API /api/search.
        #
        # Alternativa se TestConfig.APP_MODE è 'saas' e vogliamo testare con la sessione Flask:
        register_and_login_test_user(client, email="searchuser@example.com", password="testpassword")
        # Ora dovrebbe esserci una sessione Flask valida.

    mock_query_embedding = [0.1] * 768 # Embedding fittizio (768 dimensioni per text-embedding-004)
    mock_chroma_results_video = {
        'ids': [['video_chunk_1']],
        'documents': [['Testo del chunk video 1']],
        'metadatas': [[{'video_id': 'vid1', 'video_title': 'Titolo Video 1', 'source_type': 'video'}]],
        'distances': [[0.1]]
    }
    mock_chroma_results_doc = { # Nessun risultato dai documenti per questo test
        'ids': [[]], 'documents': [[]], 'metadatas': [[]], 'distances': [[]]
    }
    mock_llm_answer = "Questa è una risposta generata dall'LLM mockato basata sul contesto."

    # Percorsi dei moduli da mockare. DEVONO corrispondere al percorso usato nel file search.py
    # Se in search.py hai "from app.services.embedding.gemini_embedding import get_gemini_embeddings"
    # allora il path per patchare è "app.api.routes.search.get_gemini_embeddings"
    path_get_embeddings = 'app.api.routes.search.get_gemini_embeddings'
    path_genai_model = 'app.api.routes.search.genai.GenerativeModel' # Mockiamo la classe intera

    # Mockare le collezioni ChromaDB è un po' più complicato perché sono ottenute
    # da current_app.config. Potremmo mockare current_app.config o le chiamate .query()
    # su istanze mockate delle collezioni.
    # Per ora, proviamo a mockare le chiamate .query() direttamente.

    with patch(path_get_embeddings, return_value=[mock_query_embedding]) as mock_embed, \
         patch(path_genai_model) as MockGenerativeModel: # Patcha la classe

        # Configura l'istanza mockata di GenerativeModel e il suo metodo generate_content
        mock_llm_instance = MockGenerativeModel.return_value # L'istanza creata da GenerativeModel(...)
        mock_llm_instance.generate_content.return_value = MagicMock(text=mock_llm_answer) # Simula l'oggetto risposta con attributo .text

        # Per mockare ChromaDB:
        # Dobbiamo accedere alle collezioni come fa la route /api/search/
        # e sostituire il loro metodo .query con un MagicMock.
        # Questo è più facile se le collezioni sono già inizializzate nell'app di test.
        with app.app_context(): # Necessario per accedere a current_app.config
            mock_video_collection = MagicMock()
            mock_video_collection.query.return_value = mock_chroma_results_video

            mock_doc_collection = MagicMock()
            mock_doc_collection.query.return_value = mock_chroma_results_doc # Risultati vuoti

            mock_article_collection = MagicMock() # Mock anche per articoli, vuoto per ora
            mock_article_collection.query.return_value = {'ids': [[]], 'documents': [[]], 'metadatas': [[]], 'distances': [[]]}

            # Sostituisci le collezioni reali nella config dell'app con i nostri mock
            # Questo funziona solo se TestConfig.APP_MODE = 'single'
            # Se è 'saas', le collezioni vengono ottenute dinamicamente, il mocking è più complesso qui.
            # Assumiamo 'single' per TestConfig per questo esempio di mocking delle collezioni.
            if app.config['APP_MODE'] == 'single':
                app.config['CHROMA_VIDEO_COLLECTION'] = mock_video_collection
                app.config['CHROMA_DOC_COLLECTION'] = mock_doc_collection
                app.config['CHROMA_ARTICLE_COLLECTION'] = mock_article_collection
            else:
                # Per SAAS, dovremmo mockare chroma_client.get_collection restituendo i nostri mock_collection
                # Esempio (più avanzato, da adattare):
                # with patch('app.api.routes.search.current_app.config') as mock_config:
                #     mock_chroma_client = MagicMock()
                #     mock_config.get.side_effect = lambda key, default=None: {
                #         'CHROMA_CLIENT': mock_chroma_client,
                #         # ... altre config necessarie ...
                #     }.get(key, current_app.config.get(key, default)) # Restituisci config reali per il resto
                #     mock_chroma_client.get_collection.side_effect = lambda name: {
                #         f"{app.config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')}_searchuser@example.com": mock_video_collection,
                #         # ... ecc per doc e article ...
                #     }.get(name)
                # Questo diventa complesso rapidamente. Per ora, ci concentriamo su APP_MODE='single' in TestConfig.
                # Se TestConfig è 'saas', questo test potrebbe aver bisogno di aggiustamenti o mock più mirati.
                pytest.skip("Mocking ChromaDB collections in SAAS mode for this test is complex, skipping. Ensure TestConfig.APP_MODE is 'single' for this test or adapt mocking.")


            payload = {"query": "Test query", "n_results": 1} # n_results basso per test

            # Imposta l'header Accept per richiedere JSON (anche se requests lo fa di default)
            headers = {'Accept': 'application/json'}
            if app.config['APP_MODE'] == 'saas':
                # Per SAAS, dovremmo usare una X-API-Key valida e mockare la sua validazione
                # o usare la sessione Flask se il decoratore lo permette
                # Qui assumiamo che register_and_login_test_user abbia creato una sessione valida.
                # Se si usa X-API-Key, l'header andrebbe aggiunto qui.
                pass


            response = client.post(url_for('search.handle_search_request'), json=payload, headers=headers)

        assert response.status_code == 200
        data = response.json
        assert data['success'] is True
        assert data['answer'] == mock_llm_answer
        assert len(data['retrieved_results']) >= 1 # Dovremmo avere almeno un risultato video
        assert data['retrieved_results'][0]['metadata']['video_id'] == 'vid1'

        # Verifica che i mock siano stati chiamati
        mock_embed.assert_called_once_with(["Test query"], api_key=app.config['GOOGLE_API_KEY'], model_name=app.config['GEMINI_EMBEDDING_MODEL'], task_type='retrieval_query')
        MockGenerativeModel.assert_called_once() # Verifica che il costruttore sia stato chiamato
        mock_llm_instance.generate_content.assert_called_once() # Verifica che il metodo sia stato chiamato

        if app.config['APP_MODE'] == 'single':
            mock_video_collection.query.assert_called_once()
            mock_doc_collection.query.assert_called_once()
