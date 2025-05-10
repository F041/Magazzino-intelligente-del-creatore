# FILE: app/services/embedding/gemini_embedding.py

import logging
from typing import List, Optional, Tuple, Dict
import google.generativeai as genai
import os # Mantenuto per os.environ.get() nel caso serva altrove, ma non in queste funzioni
import time
from google.api_core import exceptions as google_exceptions
# Importa current_app qui SOLO per l'helper get_gemini_embeddings
from flask import current_app

logger = logging.getLogger(__name__)

# Task types sono costanti specifiche dell'API, OK mantenerle qui
TASK_TYPE_DOCUMENT = "retrieval_document"
TASK_TYPE_QUERY = "retrieval_query"


# --- Funzione di Chunking (NON usa current_app) ---
def split_text_into_chunks(
    text: str,
    chunk_size: int = 300,  # Default fisso se non passato
    chunk_overlap: int = 50 # Default fisso se non passato
) -> List[str]:
    """
    Divide un testo lungo in chunk più piccoli basandosi su parole.
    Usa i valori di chunk_size e chunk_overlap passati come argomenti.
    """
    # Validazione input (usa i valori passati o i default della funzione)
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        logger.warning(f"Chunk size non valido ({chunk_size}), uso default 300.")
        chunk_size = 300
    if not isinstance(chunk_overlap, int) or chunk_overlap < 0:
        logger.warning(f"Chunk overlap non valido ({chunk_overlap}), uso default 50.")
        chunk_overlap = 50
    if chunk_overlap >= chunk_size:
        logger.warning(f"Chunk overlap ({chunk_overlap}) >= chunk size ({chunk_size}). Imposto overlap a {chunk_size // 3}.")
        chunk_overlap = chunk_size // 3

    if not text: return []
    words = text.split()
    if len(words) <= chunk_size: return [text]

    chunks = []
    start_index = 0
    step = chunk_size - chunk_overlap
    step = max(1, step) # Assicura avanzamento minimo

    while start_index < len(words):
        end_index = min(start_index + chunk_size, len(words))
        chunks.append(" ".join(words[start_index:end_index]))
        start_index += step

    logger.info(f"Diviso testo ({len(words)} parole) in {len(chunks)} chunk (size={chunk_size}, overlap={chunk_overlap}).")
    return chunks


# --- Servizio Embedding (Riceve config all'init) ---
class GeminiEmbeddingService:
    """Servizio per generare embedding usando Gemini API."""

    def __init__(self, api_key: str, model_name: str): # Riceve config
        """Inizializza con API Key e nome modello forniti."""
        if not api_key: raise ValueError("API Key is required for GeminiEmbeddingService.")
        if not model_name: raise ValueError("Model name is required for GeminiEmbeddingService.")

        self.model_name = model_name
        self.api_key = api_key # Potrebbe servire salvarla se genai.configure non è globale

        try:
            # Configura l'istanza genai (questo ha effetto globale)
            # Considerare alternative se si vogliono chiavi diverse per thread/richieste diverse
            genai.configure(api_key=self.api_key)
            logger.info(f"Client Google GenAI configurato. Servizio Embedding userà modello: {self.model_name}")
        except Exception as e:
            logger.exception("Errore configurazione client Google Generative AI.")
            raise

    def get_embeddings(self, texts: List[str], task_type: Optional[str] = None) -> Optional[List[List[float]]]:
        """Genera embeddings usando il modello configurato."""
        if task_type is None: task_type = TASK_TYPE_DOCUMENT
        if task_type not in [TASK_TYPE_DOCUMENT, TASK_TYPE_QUERY]:
             logger.warning(f"Task type '{task_type}' non riconosciuto, uso '{TASK_TYPE_DOCUMENT}'.")
             task_type = TASK_TYPE_DOCUMENT
        if not texts: return []

        embeddings = []
        retries = 5
        delay = 10

        logger.info(f"Tentativo generazione embedding per {len(texts)} testi con modello {self.model_name}...")

        for i, text_batch in enumerate(self._batch_texts(texts)):
            logger.debug(f"Processo batch {i+1}/{ (len(texts) + 99) // 100 }...")
            batch_embeddings = None # Inizializza per controllo
            for attempt in range(retries):
                try:
                    result = genai.embed_content(
                        model=self.model_name, # Usa il modello salvato nell'istanza
                        content=text_batch,
                        task_type=task_type
                    )
                    batch_embeddings = result.get('embedding', [])
                    if batch_embeddings:
                        logger.debug(f"Ottenuti {len(batch_embeddings)} embedding per il batch {i+1}.")
                        break # Successo per questo batch
                    else:
                        logger.error("Risposta API embed_content non valida (manca 'embedding') per batch %d.", i+1)
                        if attempt == retries - 1: logger.error(f"Fallimento persistente batch {i+1}."); return None
                        else: time.sleep(delay); delay *= 1.5

                except google_exceptions.ResourceExhausted as e:
                    logger.warning(f"Rate limit API (batch {i+1}, tentativo {attempt + 1}/{retries}). Attesa {int(delay)}s...")
                    time.sleep(delay); delay *= 1.5
                    if attempt == retries - 1: logger.error(f"Rate limit superato dopo {retries} tentativi per batch {i+1}."); return None
                except Exception as e:
                    logger.exception(f"Errore imprevisto chiamata embed_content (batch {i+1}, tentativo {attempt + 1}/{retries}).")
                    time.sleep(delay); delay *= 1.5
                    if attempt == retries - 1: logger.error(f"Errore API/rete persistente dopo {retries} tentativi per batch {i+1}."); return None
            # Fine ciclo retry
            if not batch_embeddings: # Se tutti i tentativi sono falliti e non abbiamo embeddings
                 logger.error(f"Tutti i {retries} tentativi di retry falliti per batch {i+1}. Interruzione.")
                 return None
            embeddings.extend(batch_embeddings) # Aggiungi il batch valido alla lista totale

        # Verifica finale
        if len(embeddings) == len(texts):
            logger.info(f"Generazione embedding completata con successo per {len(texts)} testi.")
            return embeddings
        else:
             logger.error(f"Errore finale: numero embedding ({len(embeddings)}) != numero testi ({len(texts)}).")
             return None

    def _batch_texts(self, texts: List[str], batch_size: int = 100) -> List[List[str]]:
        """Divide la lista di testi in batch."""
        for i in range(0, len(texts), batch_size):
            yield texts[i:i + batch_size]


# --- Funzione Helper (Modificata per ACCETTARE config e passarla) ---
def get_gemini_embeddings(
    texts: List[str],
    api_key: str,      
    model_name: str,   
    task_type: Optional[str] = None
) -> Optional[List[List[float]]]:
    """
    Helper per ottenere embedding. Richiede api_key e model_name espliciti.
    Crea un'istanza di GeminiEmbeddingService e chiama il suo metodo.
    Restituisce None in caso di errore.
    """
    try:
        # Verifica gli argomenti ricevuti
        if not api_key: raise ValueError("API Key mancante in chiamata a get_gemini_embeddings.")
        if not model_name: raise ValueError("Model Name mancante in chiamata a get_gemini_embeddings.")

        # Crea istanza del servizio PASSANDO la config ricevuta
        service = GeminiEmbeddingService(api_key=api_key, model_name=model_name)
        # Chiama il metodo dell'istanza creata
        return service.get_embeddings(texts, task_type=task_type)
    except (ValueError, RuntimeError, Exception) as e: # Cattura errori creazione servizio o embedding
        # Logga l'errore completo per debug
        logger.error(f"Fallimento in get_gemini_embeddings: {e}", exc_info=True)
        return None # Segnala errore restituendo None