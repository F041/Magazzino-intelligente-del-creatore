import pytest
from unittest.mock import patch, MagicMock

# Importiamo la funzione che vogliamo testare
from app.services.embedding.embedding_service import generate_embeddings
from app.services.embedding.gemini_embedding import TASK_TYPE_DOCUMENT

# Definiamo i path delle funzioni che dovremo "ingannare" (mockare)
# Usiamo i loro percorsi completi a partire dalla radice del progetto
path_get_google_embeddings = 'app.services.embedding.embedding_service.get_gemini_embeddings'
path_get_ollama_embeddings = 'app.services.embedding.embedding_service._get_ollama_embeddings'

# --- Test Scenario 1: Il centralinista chiama Google ---
def test_generate_embeddings_chooses_google_by_default(app):
    """
    Verifica che se il provider è 'google', venga chiamata la funzione di embedding di Gemini.
    """
    # ARRANGE: Prepariamo le impostazioni e i nostri "attori" finti
    user_settings = {
        'llm_provider': 'google',
        'llm_api_key': 'fake_google_key'
    }
    test_texts = ["Questo è un testo di prova."]

    # Usiamo 'patch' per sostituire temporaneamente le vere funzioni con delle "spie"
    with patch(path_get_google_embeddings, return_value=[[0.1]*768]) as mock_google_func, \
         patch(path_get_ollama_embeddings) as mock_ollama_func:
        
        # ACT: Eseguiamo la funzione
        with app.app_context(): # Serve per `current_app.config` dentro la funzione
            generate_embeddings(texts=test_texts, user_settings=user_settings, task_type=TASK_TYPE_DOCUMENT)

        # ASSERT: Facciamo le nostre verifiche
        mock_google_func.assert_called_once()  # La spia di Google DEVE essere stata chiamata
        mock_ollama_func.assert_not_called()   # La spia di Ollama NON deve essere stata chiamata

# --- Test Scenario 2: Il centralinista chiama Ollama ---
def test_generate_embeddings_chooses_ollama_when_configured(app):
    """
    Verifica che se il provider è 'ollama' E il modello di embedding è specificato,
    venga chiamata la funzione di embedding di Ollama.
    """
    # ARRANGE
    user_settings = {
        'llm_provider': 'ollama',
        'llm_embedding_model': 'nomic-embed-text', # Modello specificato
        'ollama_base_url': 'http://fake-ollama'
    }
    test_texts = ["Questo è un altro testo di prova."]

    with patch(path_get_google_embeddings) as mock_google_func, \
         patch(path_get_ollama_embeddings, return_value=[[0.2]*512]) as mock_ollama_func:
        
        # ACT
        with app.app_context():
            generate_embeddings(texts=test_texts, user_settings=user_settings, task_type=TASK_TYPE_DOCUMENT)

        # ASSERT
        mock_google_func.assert_not_called()
        mock_ollama_func.assert_called_once()
        # Verifichiamo anche che sia stata chiamata con i parametri giusti
        mock_ollama_func.assert_called_with(test_texts, 'http://fake-ollama', 'nomic-embed-text')

# --- Test Scenario 3: Il centralinista torna a Google per sicurezza ---
def test_generate_embeddings_falls_back_to_google_if_ollama_model_is_missing(app):
    """
    Verifica che se il provider è 'ollama' MA manca il modello di embedding,
    il sistema usi Google come opzione di sicurezza (fallback).
    """
    # ARRANGE
    user_settings = {
        'llm_provider': 'ollama',
        'llm_embedding_model': None, # Modello NON specificato
        'ollama_base_url': 'http://fake-ollama'
    }
    test_texts = ["Testo di fallback."]

    with patch(path_get_google_embeddings, return_value=[[0.3]*768]) as mock_google_func, \
         patch(path_get_ollama_embeddings) as mock_ollama_func:
        
        # ACT
        with app.app_context():
            generate_embeddings(texts=test_texts, user_settings=user_settings, task_type=TASK_TYPE_DOCUMENT)

        # ASSERT
        mock_google_func.assert_called_once() # DEVE chiamare Google
        mock_ollama_func.assert_not_called()  # NON deve chiamare Ollama