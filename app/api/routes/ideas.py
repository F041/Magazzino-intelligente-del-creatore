import logging
import sqlite3
import os
import time 
import requests
import random # Ci servirà per pescare chunk casuali
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from flask import Blueprint, jsonify, current_app
from app.api.routes.search import _get_ollama_completion 
from flask_login import login_required, current_user

logger = logging.getLogger(__name__)
ideas_bp = Blueprint('ideas', __name__)

def _get_random_chunks_from_collections(user_id, config, sample_size=20):
    """
    Recupera un campione casuale di chunk di testo dalle collezioni ChromaDB dell'utente.
    """
    chroma_client = config.get('CHROMA_CLIENT')
    if not chroma_client:
        logger.warning("Chroma Client non disponibile per il recupero dei chunk.")
        return []

    app_mode = config.get('APP_MODE', 'single')
    all_chunks = []
    
    # Nomi base delle collezioni
    base_names = {
        "video": config.get('VIDEO_COLLECTION_NAME', 'video_transcripts'),
        "document": config.get('DOCUMENT_COLLECTION_NAME', 'document_content'),
        "article": config.get('ARTICLE_COLLECTION_NAME', 'article_content'),
        "page": "page_content"
    }

    for base_name in base_names.values():
        collection_name = f"{base_name}_{user_id}" if app_mode == 'saas' else base_name
        try:
            collection = chroma_client.get_collection(name=collection_name)
            # Il metodo .get() di ChromaDB recupera i dati. Usiamo include=["documents"]
            # per ottenere solo il testo dei chunk, rendendo la chiamata più leggera.
            collection_data = collection.get(include=["documents"])
            if collection_data and collection_data['documents']:
                all_chunks.extend(collection_data['documents'])
        except Exception as e:
            logger.debug(f"Collezione '{collection_name}' non trovata o vuota durante la ricerca di idee. ({e})")

    if not all_chunks:
        return []
    
    # Mescola e restituisci un campione. Se ci sono meno chunk di sample_size, li restituisce tutti.
    random.shuffle(all_chunks)
    return all_chunks[:sample_size]

def _generate_content_ideas_core(user_id, config):
    """
    Funzione interna che analizza un campione di contenuti ESISTENTI di un utente 
    e usa un LLM per generare nuove idee pertinenti, rispettando le impostazioni utente.
    """
    # --- INIZIO BLOCCO CRONOMETRI ---
    import time
    total_start_time = time.time()
    performance_metrics = {}
    # --- FINE BLOCCO CRONOMETRI ---

    conn = None
    try:
        # --- RECUPERO DELLE IMPOSTAZIONI AI DELL'UTENTE ---
        llm_provider = 'google' # Default
        llm_api_key = config.get('GOOGLE_API_KEY') # Chiave di default dal .env
        ollama_base_url = None
        models_to_try = config.get('RAG_MODELS_LIST')
        
        db_path_settings = config.get('DATABASE_FILE')
        conn_settings = None
        if user_id and db_path_settings:
            try:
                conn_settings = sqlite3.connect(db_path_settings)
                conn_settings.row_factory = sqlite3.Row
                cursor_settings = conn_settings.cursor()
                cursor_settings.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
                user_settings = cursor_settings.fetchone()
                
                if user_settings:
                    logger.info(f"Trovate impostazioni AI personalizzate per l'utente {user_id}.")
                    llm_provider = user_settings['llm_provider'] or 'google'
                    ollama_base_url = user_settings['ollama_base_url']
                    if user_settings['llm_api_key']: 
                        llm_api_key = user_settings['llm_api_key']
                    if user_settings['llm_model_name']:
                        models_to_try = [m.strip() for m in user_settings['llm_model_name'].split(',') if m.strip()]
            except sqlite3.Error as e:
                logger.error(f"Errore DB nel recuperare le impostazioni AI per l'utente {user_id}: {e}")
            finally:
                if conn_settings: conn_settings.close()
        
        if not models_to_try:
             models_to_try = config.get('RAG_MODELS_LIST', ["gemini-1.5-pro-latest"])
             if not models_to_try:
                 models_to_try = ["gemini-1.5-pro-latest"]
        # --- FINE RECUPERO IMPOSTAZIONI ---

        # --- FASE 1: RECUPERO CHUNK (con misurazione) ---
        start_retrieval_time = time.time()
        content_samples = _get_random_chunks_from_collections(user_id, config, sample_size=25)
        performance_metrics['retrieval_duration_ms'] = round((time.time() - start_retrieval_time) * 1000)
        performance_metrics['retrieved_chunks_count'] = len(content_samples)
        logger.info(f"Recuperati {len(content_samples)} chunk per idee in {performance_metrics['retrieval_duration_ms']}ms.")


        if not content_samples:
            # Aggiungiamo le performance anche qui
            performance_metrics['total_duration_ms'] = round((time.time() - total_start_time) * 1000)
            return {
                "success": True, 
                "ideas": "Non ho ancora trovato tuoi contenuti da analizzare. Carica qualche video, documento o articolo e poi chiedimi di nuovo delle idee!",
                "performance_metrics": performance_metrics
            }

        # 2. Prepara il prompt per l'LLM con il tuo incipit
        context_for_prompt = "\n\n---\n\n".join(content_samples)
        
        if llm_provider == 'google' and not llm_api_key:
            raise ValueError("GOOGLE_API_KEY non configurata o mancante per l'utente.")
        
        if llm_provider == 'google':
            genai.configure(api_key=llm_api_key)

        prompt = f"""
        Fai il content strategist, come ad esempio Riccardo Belleggia di Loop SRL: un partner creativo per imprenditori, proprietari e creator.
        Compito: generare idee efficaci e originali per nuovi contenuti.

        Ho analizzato dei passaggi chiave estratti casualmente dai contenuti che l'utente ha già creato per capire i suoi argomenti principali, il suo stile e il suo tono di voce.
        Ecco i campioni di testo:
        ---
        {context_for_prompt}
        ---

        Basandoti sull'essenza di questi testi, il tuo compito è generare 5 nuove idee di contenuti che siano una naturale e interessante evoluzione del suo lavoro. 
        Sii creativo e audace: non limitarti a ripetere gli argomenti esistenti, ma espandili, collegali in modi nuovi o identifica un argomento correlato che potrebbe interessare al suo pubblico.

        Per ogni idea, fornisci:
        - Un titolo che parla la lingua del target di riferimento dei contenuti. **REGOLA FONDAMENTALE PER I TITOLI:** formatta i titoli seguendo lo stile italiano ("sentence case"), dove solo la prima parola è maiuscola. Esempio CORRETTO: "Come usare i dati per il tuo business". Esempio SBAGLIATO: "Come Usare i Dati per il Tuo Business".
        - Una breve descrizione (5-12 frasi) che spieghi l'idea e perché è di valore.
        - Hook: 3 idee per catturare l'attenzione nei primi 5 secondi **CRUCIALI**.
        - Il formato suggerito (es. video-tutorial, articolo di approfondimento, podcast, short/reel).
        - Idea thumbnail/visual: (1 riga)  

        Formatta la tua risposta in Markdown. Usa un titolo di livello 2 (##) per ogni idea.
        Inizia la tua risposta con una frase incoraggiante.
        """

        generated_ideas = None
        last_error = None
        successful_model = "N/D"

        # --- FASE 2: GENERAZIONE LLM (con misurazione) ---
        start_generation_time = time.time()
        
        if llm_provider == 'ollama':
            ollama_model = models_to_try[0] if models_to_try else None
            if not ollama_base_url or not ollama_model:
                raise RuntimeError("Impostazioni Ollama (URL o nome modello) non configurate per l'utente.")
            try:
                # Chiamata a Ollama
                
                generated_ideas = _get_ollama_completion(prompt, ollama_base_url, ollama_model)
                successful_model = ollama_model
                logger.info(f"Idee generate con successo da Ollama con il modello {ollama_model}.")
            except Exception as e:
                last_error = e
        else: # Provider Google Gemini (o default)
            for model_name in models_to_try:
                logger.info(f"Tentativo generazione idee con il modello Google: {model_name}")
                try:
                    model = genai.GenerativeModel(model_name)
                    response = model.generate_content(prompt)
                    
                    try:
                        generated_ideas = response.text
                        successful_model = model_name
                        logger.info(f"Idee generate con successo dal modello Google {model_name}.")
                        break
                    except ValueError:
                        block_reason_obj = getattr(getattr(response, 'prompt_feedback', None), 'block_reason', None)
                        block_reason_name = getattr(block_reason_obj, 'name', 'UNKNOWN_REASON')
                        logger.warning(f"Generazione idee bloccata dal modello Google {model_name} per motivo: {block_reason_name}. Tento con il prossimo.")
                        last_error = ValueError(f"Blocked by model {model_name}")
                        continue
                except (google_exceptions.NotFound, google_exceptions.PermissionDenied, 
                        google_exceptions.InternalServerError, google_exceptions.ResourceExhausted) as e_fallback:
                    last_error = e_fallback
                    logger.warning(f"Modello Google '{model_name}' non accessibile o quota superata. Tento con il prossimo. Errore: {e_fallback}")
                    continue
                except Exception as e_gen:
                    last_error = e_gen
                    logger.error(f"Errore inatteso durante la generazione con {model_name}: {e_gen}", exc_info=True)
                    break

        performance_metrics['llm_generation_duration_ms'] = round((time.time() - start_generation_time) * 1000)
        performance_metrics['llm_model_used'] = successful_model
        logger.info(f"Generazione LLM per idee completata in {performance_metrics['llm_generation_duration_ms']}ms.")

        # --- AGGIUNTA FINALE DELLE METRICHE ALLA RISPOSTA ---
        performance_metrics['total_duration_ms'] = round((time.time() - total_start_time) * 1000)

        if generated_ideas:
            return {"success": True, "ideas": generated_ideas, "model_used": successful_model, "performance_metrics": performance_metrics}
        else:
            error_message = "Impossibile generare idee dopo aver provato tutti i modelli disponibili."
            if last_error:
                error_message += f" Errore finale: {last_error}"
            return {"success": False, "error": error_message, "performance_metrics": performance_metrics}


    except Exception as e:
        logger.error(f"Errore critico in _generate_content_ideas_core per l'utente {user_id}: {e}", exc_info=True)
        # Aggiungiamo le performance anche in caso di errore
        performance_metrics['total_duration_ms'] = round((time.time() - total_start_time) * 1000)
        return {"success": False, "error": f"Si è verificato un errore critico: {e}", "performance_metrics": performance_metrics}
    finally:
        if conn:
            conn.close()

@ideas_bp.route('/generate', methods=['GET'])
@login_required
def generate_content_ideas_endpoint():
    """
    Endpoint API per avviare la generazione di idee di contenuti basata sui contenuti esistenti.
    """
    logger.info(f"Richiesta di generazione idee per l'utente {current_user.id}")
    config = current_app.config 
    
    result = _generate_content_ideas_core(current_user.id, config)
    
    if result.get("success"):
        return jsonify(result), 200
    else:
        return jsonify(result), 500