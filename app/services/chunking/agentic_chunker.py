import logging
import json
import re
import requests
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

logger = logging.getLogger(__name__)

AGENTIC_CHUNKER_PROMPT_TEMPLATE = """
Sei un assistente esperto nell'analisi di documenti. Il tuo compito è suddividere il testo fornito in sezioni o "chunk" semanticamente coerenti.
**Istruzioni:**
1.  Leggi attentamente l'intero testo per capirne la struttura e gli argomenti principali.
2.  Identifica i punti di rottura logici. Un buon punto di rottura si trova dove l'argomento principale cambia, o alla fine di un concetto completo.
3.  Ogni chunk che produci deve avere un senso compiuto se letto da solo. Non tagliare mai una frase a metà.
4.  Cerca di creare chunk che non siano né troppo corti (inutili) né troppo lunghi (troppo generici). Una buona lunghezza è tra le 150 e le 400 parole, ma la coerenza semantica è più importante della lunghezza.
**Formato di output:**
Restituisci la tua risposta come un array JSON di stringhe, dove ogni stringa è un singolo chunk di testo. Non includere nient'altro nella tua risposta al di fuori dell'array JSON.
**Esempio di output corretto:**
["Primo chunk di testo che ha senso da solo.", "Secondo chunk di testo che parla di un altro argomento."]
**Testo da suddividere:**
---
{text_to_chunk}
---
"""

def chunk_text_agentically(text_to_chunk: str, llm_provider: str, settings: dict) -> list[str]:
    """
    Usa un LLM per suddividere un testo, con logica di fallback intelligente per la selezione del modello.
    """
    if not text_to_chunk or not text_to_chunk.strip():
        return []

    final_prompt = AGENTIC_CHUNKER_PROMPT_TEMPLATE.format(text_to_chunk=text_to_chunk)
    raw_llm_response = ""

    try:
        if llm_provider == 'ollama':
            # ... (la logica per Ollama non cambia) ...
            base_url = settings.get('ollama_base_url')
            model_name = settings.get('llm_model_name')
            if not base_url or not model_name:
                raise ValueError("URL o nome modello di Ollama non forniti.")
            
            api_url = base_url.rstrip('/') + "/api/generate"
            payload = {"model": model_name, "prompt": final_prompt, "stream": False, "format": "json"}
            
            logger.info(f"Agentic Chunker: Invio richiesta a Ollama (Modello: {model_name})")
            response = requests.post(api_url, json=payload, timeout=180)
            response.raise_for_status()
            raw_llm_response = response.json().get("response", "")

        elif llm_provider == 'google':
            api_key = settings.get('GOOGLE_API_KEY')
            
            # --- INIZIO NUOVA LOGICA DI SELEZIONE MODELLO ---
            user_models = settings.get('RAG_MODELS_LIST', [])
            default_models_from_env = settings.get('DEFAULT_RAG_MODELS_LIST_FROM_ENV', [])
            
            model_to_use = None
            # Priorità 1: Il modello di ripiego dell'utente (se specificato)
            if len(user_models) > 1 and user_models[1].strip():
                model_to_use = user_models[1].strip()
                logger.info(f"Agentic Chunker: Selezionato modello di ripiego specificato dall'utente: '{model_to_use}'.")
            # Priorità 2: Il modello di ripiego del sistema (se l'utente ha specificato un solo modello pro)
            elif len(user_models) == 1 and len(default_models_from_env) > 1 and default_models_from_env[1].strip():
                model_to_use = default_models_from_env[1].strip()
                logger.info(f"Agentic Chunker: L'utente ha specificato un solo modello. Uso il fallback di sistema per ottimizzare: '{model_to_use}'.")
            # Priorità 3: Il primo (e unico) modello dell'utente
            elif user_models and user_models[0].strip():
                model_to_use = user_models[0].strip()
                logger.info(f"Agentic Chunker: Nessun ripiego disponibile. Uso il modello primario dell'utente: '{model_to_use}'.")
            # Priorità 4: Il primo modello di default del sistema
            elif default_models_from_env and default_models_from_env[0].strip():
                model_to_use = default_models_from_env[0].strip()
                logger.info(f"Agentic Chunker: Nessuna configurazione utente. Uso il modello primario di sistema: '{model_to_use}'.")
            
            if not api_key or not model_to_use:
                raise ValueError("API Key di Google o un modello valido non sono stati determinati per il chunking.")
            # --- FINE NUOVA LOGICA DI SELEZIONE MODELLO ---

            genai.configure(api_key=api_key)

            logger.info(f"Agentic Chunker: Invio richiesta a Google Gemini (Modello: {model_to_use})")
            model = genai.GenerativeModel(model_to_use)
            response = model.generate_content(
                final_prompt,
                generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
            )
            raw_llm_response = response.text
        
        else:
            logger.error(f"Provider LLM non supportato: {llm_provider}")
            return []

        json_string = re.sub(r'^```json\s*|\s*```$', '', raw_llm_response, flags=re.DOTALL).strip()
        chunks = json.loads(json_string)
        
        if not isinstance(chunks, list) or not all(isinstance(c, str) for c in chunks):
            logger.error("La risposta JSON non è un array di stringhe.")
            return []
            
        logger.info(f"Agentic Chunker: Testo suddiviso in {len(chunks)} chunk.")
        return chunks
    
    except google_exceptions.ResourceExhausted as e:
        logger.warning(f"Agentic Chunker: Rilevato rate limit. Lo segnalo allo script chiamante.")
        raise e
    except json.JSONDecodeError:
        logger.error("Agentic Chunker: Errore di decodifica JSON.")
        return []
    except Exception as e:
        logger.error(f"Agentic Chunker: Errore finale: {e}", exc_info=True)
        raise e