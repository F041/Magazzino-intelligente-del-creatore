
# ATTENZIONE NON TESTATO!
import logging
import json
import re
import requests
import google.generativeai as genai

logger = logging.getLogger(__name__)

# Il prompt che abbiamo definito per istruire il nostro agente.
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
    Usa un LLM per suddividere un testo in chunk semanticamente coerenti.
    
    Args:
        text_to_chunk: Il testo da suddividere.
        llm_provider: 'google' o 'ollama'.
        settings: Un dizionario contenente le configurazioni necessarie (API key, URL, nome modello).
        
    Returns:
        Una lista di stringhe, dove ogni stringa è un chunk.
        Restituisce una lista vuota in caso di errore.
    """
    final_prompt = AGENTIC_CHUNKER_PROMPT_TEMPLATE.format(text_to_chunk=text_to_chunk)
    raw_llm_response = ""

    try:
        if llm_provider == 'ollama':
            base_url = settings.get('ollama_base_url')
            model_name = settings.get('llm_model_name')
            if not base_url or not model_name:
                raise ValueError("URL o nome modello di Ollama non forniti per l'agentic chunker.")
            
            api_url = base_url.rstrip('/') + "/api/generate"
            payload = {"model": model_name, "prompt": final_prompt, "stream": False}
            
            logger.info(f"Agentic Chunker: Invio richiesta a Ollama (Modello: {model_name})")
            response = requests.post(api_url, json=payload, timeout=180) # Timeout più lungo per chunking
            response.raise_for_status()
            raw_llm_response = response.json().get("response", "")

        elif llm_provider == 'google':
            api_key = settings.get('llm_api_key')
            model_name = settings.get('llm_model_name_primary') # Usiamo il modello primario
            if not api_key or not model_name:
                raise ValueError("API Key o nome modello di Google non forniti per l'agentic chunker.")
            
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(model_name)
            
            logger.info(f"Agentic Chunker: Invio richiesta a Google Gemini (Modello: {model_name})")
            response = model.generate_content(final_prompt)
            raw_llm_response = response.text
        
        else:
            logger.error(f"Provider LLM non supportato per l'agentic chunker: {llm_provider}")
            return []

        # --- Pulizia e Parsing della risposta JSON ---
        logger.debug(f"Agentic Chunker: Risposta grezza dal LLM:\n{raw_llm_response}")
        
        # Rimuovi i "recinti" del codice markdown se l'LLM li aggiunge
        json_match = re.search(r'\[.*\]', raw_llm_response, re.DOTALL)
        if not json_match:
            logger.error("Agentic Chunker: L'LLM non ha restituito un array JSON valido.")
            return []
            
        json_string = json_match.group(0)
        
        # Tenta di parsare la stringa JSON in una lista Python
        chunks = json.loads(json_string)
        
        if not isinstance(chunks, list) or not all(isinstance(c, str) for c in chunks):
            logger.error("Agentic Chunker: Il JSON restituito non è un array di stringhe.")
            return []
            
        logger.info(f"Agentic Chunker: Testo suddiviso con successo in {len(chunks)} chunk intelligenti.")
        return chunks

    except json.JSONDecodeError:
        logger.error("Agentic Chunker: Errore di decodifica JSON. La risposta dell'LLM non era formattata correttamente.")
        return []
    except Exception as e:
        logger.error(f"Agentic Chunker: Si è verificato un errore: {e}", exc_info=True)
        return []