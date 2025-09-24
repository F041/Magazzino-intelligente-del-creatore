import logging
import requests
from typing import List, Optional
from flask import current_app

# Importiamo le funzioni che già abbiamo per non riscrivere codice
from .gemini_embedding import get_gemini_embeddings, TASK_TYPE_QUERY, TASK_TYPE_DOCUMENT

logger = logging.getLogger(__name__)

def _get_ollama_embeddings(texts: List[str], base_url: str, model_name: str) -> Optional[List[List[float]]]:
    """Genera embeddings per una lista di testi usando un'API Ollama."""
    if not base_url.endswith('/'):
        base_url += '/'
    api_url = f"{base_url}api/embeddings"
    
    all_embeddings = []
    logger.info(f"Invio {len(texts)} richieste di embedding a Ollama (una per una)...")
    
    for text in texts:
        payload = {"model": model_name, "prompt": text}
        try:
            response = requests.post(api_url, json=payload, timeout=60)
            response.raise_for_status()
            response_data = response.json()
            embedding = response_data.get("embedding")
            if embedding:
                all_embeddings.append(embedding)
            else:
                logger.warning(f"Ollama non ha restituito un embedding per il testo: {text[:50]}...")
                return None # Se anche solo uno fallisce, interrompiamo per coerenza
        except Exception as e:
            logger.error(f"Errore durante l'embedding con Ollama per il testo '{text[:50]}...': {e}", exc_info=True)
            return None # Interrompi al primo errore
            
    return all_embeddings

def generate_embeddings(texts: List[str], user_settings: dict, task_type: str = TASK_TYPE_DOCUMENT) -> Optional[List[List[float]]]:
    """
    Funzione "intelligente" che genera embeddings scegliendo il provider corretto
    (Google o Ollama) in base alle impostazioni dell'utente.
    """
    llm_provider = user_settings.get('llm_provider')
    embedding_model_ollama = user_settings.get('llm_embedding_model')
    ollama_base_url = user_settings.get('ollama_base_url')

    if llm_provider == 'ollama' and embedding_model_ollama and ollama_base_url:
        logger.info(f"Usando Ollama per embedding con il modello: {embedding_model_ollama}")
        return _get_ollama_embeddings(texts, ollama_base_url, embedding_model_ollama)
    else:
        logger.info(f"Usando Google Gemini per embedding.")
        google_api_key = user_settings.get('llm_api_key') or current_app.config.get('GOOGLE_API_KEY')
        google_embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
        
        return get_gemini_embeddings(texts, api_key=google_api_key, model_name=google_embedding_model, task_type=task_type)