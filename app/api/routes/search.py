
import logging
from flask import Blueprint, request, jsonify, current_app, Response, stream_with_context
from flask_login import current_user # Non serve login_required qui se protetto da nostro decoratore
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import List, Dict, Optional
from functools import wraps
import sqlite3
import json
import time

# Import robusto
try:
    from app.services.embedding.gemini_embedding import get_gemini_embeddings, TASK_TYPE_QUERY
except ImportError:
    logger = logging.getLogger(__name__) # Assicura definizione
    logger.error("!!! Impossibile importare get_gemini_embeddings (search.py) !!!")
    get_gemini_embeddings = None
    TASK_TYPE_QUERY = "retrieval_query"

logger = logging.getLogger(__name__)
search_bp = Blueprint('search', __name__)

# --- Funzione Helper SSE ---
def format_sse_event(data_dict: dict, event_type: str = 'status') -> str:
    json_data = json.dumps(data_dict)
    return f"event: {event_type}\ndata: {json_data}\n\n"

# --- Decoratore Modificato ---
def require_api_key(f):
    """
    Decoratore per proteggere le API.
    - In modalità 'saas', richiede sessione Flask valida o X-API-Key valida.
    - In modalità 'single', permette accesso senza autenticazione.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        app_mode = current_app.config.get('APP_MODE', 'single')
        if app_mode == 'single':
            logger.debug("Decoratore @require_api_key: Modalità SINGLE. Accesso consentito.")
            return f(*args, **kwargs)

        logger.debug("Decoratore @require_api_key: Modalità SAAS. Controllo API Key/Sessione...")
        provided_key = request.headers.get('X-API-Key')
        if provided_key:
            logger.debug(f"API Key fornita: {provided_key[:5]}...")
            user_id_for_api = None; key_name = None
            db_path = current_app.config.get('DATABASE_FILE'); conn = None
            try:
                if not db_path: raise ValueError("DB_PATH mancante")
                conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
                cursor.execute("SELECT user_id, name FROM api_keys WHERE key = ? AND is_active = TRUE", (provided_key,))
                key_data = cursor.fetchone()
                if key_data:
                    user_id_for_api = key_data['user_id']; key_name = key_data['name']
                    logger.info(f"API Key valida. User ID: {user_id_for_api} (Nome: {key_name or 'N/D'}).")
                    try: cursor.execute("UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE key = ?", (provided_key,)); conn.commit(); logger.debug("Timestamp aggiornato.")
                    except: conn.rollback() # Ignora errore update timestamp
                else:
                    logger.warning(f"API Key ('{provided_key[:5]}...') non valida/attiva."); return jsonify({"success": False, "error_code": "UNAUTHORIZED", "message": "Chiave API non valida o revocata."}), 401
            except Exception as db_err: logger.error(f"Errore DB validazione API Key: {db_err}"); return jsonify({"success": False, "error_code": "DB_ERROR", "message": "Errore database validazione chiave."}), 500
            finally:
                if conn: conn.close()
            if user_id_for_api: kwargs['api_user_id_override'] = user_id_for_api; return f(*args, **kwargs)
            else: return jsonify({"success": False, "error_code": "INTERNAL_SERVER_ERROR", "message": "Errore determinazione utente da chiave API."}), 500
        else: # No API Key, check session
            if current_user.is_authenticated: logger.debug("Accesso API via sessione Flask."); return f(*args, **kwargs)
            else: logger.warning("Accesso API negato: No API Key e No sessione."); return jsonify({"success": False, "error_code": "UNAUTHORIZED", "message": "Autenticazione richiesta."}), 401
    return decorated_function

# --- Funzione build_prompt (INVARIATA, la includo per completezza) ---
def build_prompt(query: str, context_chunks: List[Dict]) -> str:
    if not context_chunks:
        prompt = f"""Sei l'assistente AI del "Magazzino del creatore".
        Rispondi brevemente alla seguente domanda basandoti sulle tue conoscenze generali,
        ma specifica che non hai trovato contesto specifico nei documenti analizzati.

Domanda: {query}

Risposta (senza contesto specifico):"""
        logger.warning("Nessun chunk di contesto recuperato per la query. Uso prompt generico.")
        return prompt
    context = "\n---\n".join([chunk.get('text', '') for chunk in context_chunks])
    prompt = f"""Sei l'assistente AI del progetto "Magazzino del creatore",
    un archivio di contenuti video e testuali.
    Il tuo compito è rispondere alle domande dell'utente basandoti
    **SOLTANTO ED ESCLUSIVAMENTE** sulle informazioni presenti nel "Contesto fornito"
    qui sotto. Non aggiungere informazioni esterne o tue conoscenze generali.

**Istruzioni importanti:**
1.  Leggi attentamente la "Domanda dell'utente".
2.  Trova le informazioni pertinenti SOLO nel "Contesto fornito".
3.  Formula una risposta chiara usando SOLO le informazioni trovate.
4.  Se il contesto fornito non contiene informazioni sufficienti per rispondere alla domanda, rispondi ESATTAMENTE con: "Le informazioni recuperate non contengono una risposta diretta a questa domanda." Non aggiungere altro in questo caso.
5.  Non inventare risposte. La fedeltà al contesto è la priorità assoluta.

**Contesto fornito:**
---
{context}
---

**Domanda dell'utente:** {query}

**Risposta (basata esclusivamente sul contesto):**"""
    # logger.debug(f"Prompt costruito per LLM:\n{prompt}") # Troppo verboso
    return prompt


# --- Funzione generatore SSE perform_search_sse ---
@search_bp.route('/', methods=['POST'])
@require_api_key # Usa il decoratore modificato
def perform_search_sse(*args, **kwargs):
    query_text_for_log = "N/D" # Per log iniziale

    def generate_events():
        nonlocal query_text_for_log
        final_payload = { "success": False, "answer": None, "retrieved_results": [], "error_code": None, "message": None }
        query_text_internal = None

        try:
            # --- 1. Recupero e Validazione Input ---
            if not request.is_json:
                final_payload.update({'error_code': 'INVALID_CONTENT_TYPE', 'message': 'Richiesta deve essere JSON.'})
                yield format_sse_event(final_payload, event_type='error_final'); return
            data = request.get_json()
            query_text_internal = data.get('query')
            query_text_for_log = query_text_internal[:100] if query_text_internal else "Query non fornita"

            if not query_text_internal or not isinstance(query_text_internal, str) or not query_text_internal.strip():
                 final_payload.update({'error_code': 'VALIDATION_ERROR', 'message': "Testo della domanda mancante o non valido."})
                 yield format_sse_event(final_payload, event_type='error_final'); return

            default_n_results = current_app.config.get('RAG_DEFAULT_N_RESULTS', 7); n_results = default_n_results
            n_results_raw = data.get('n_results', default_n_results)
            try: n_results_temp = int(n_results_raw); n_results = n_results_temp if 0 < n_results_temp <= 50 else default_n_results
            except: n_results = default_n_results # Ignora errori e usa default
            logger.info(f"Query SSE: '{query_text_for_log}', n_results={n_results}")

            yield format_sse_event({'message': 'Analisi domanda...'})

            # --- 2. Verifica Servizio Embedding ---
            if not get_gemini_embeddings:
                 final_payload.update({'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Servizio Embedding non disponibile.'})
                 yield format_sse_event(final_payload, event_type='error_final'); return

            # --- 3. Generazione Embedding Query ---
            yield format_sse_event({'message': 'Creazione rappresentazione semantica...'})
            query_embedding = None
            try:
                llm_api_key=current_app.config.get('GOOGLE_API_KEY'); embedding_model=current_app.config.get('GEMINI_EMBEDDING_MODEL')
                if not llm_api_key or not embedding_model: raise RuntimeError("Config API Key/Model mancante.")
                query_embedding_list = get_gemini_embeddings([query_text_internal], api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_QUERY)
                if not query_embedding_list: raise RuntimeError("Fallimento embedding (None).")
                query_embedding = query_embedding_list[0]; logger.info("Embedding query generato.")
            except Exception as e: # Cattura errori embedding
                error_code = 'EMBEDDING_UNEXPECTED_ERROR'; message = f'Errore embedding: {e}'
                # ... (gestione errori specifici API Google come prima) ...
                if isinstance(e, google_exceptions.ResourceExhausted): error_code = 'API_RATE_LIMIT_EMBEDDING'; message = 'Limite API embedding.'
                elif isinstance(e, google_exceptions.GoogleAPIError): error_code = 'API_ERROR_EMBEDDING'; message = f'Errore API Google embedding ({getattr(e, "code", "N/A")}).'
                elif isinstance(e, RuntimeError): error_code = 'EMBEDDING_GENERATION_FAILED'; message = str(e)
                final_payload.update({'error_code': error_code, 'message': message}); yield format_sse_event(final_payload, event_type='error_final'); return

            # --- 4. Ottenimento Collezioni ChromaDB ---
            yield format_sse_event({'message': 'Accesso base di conoscenza...'})
            app_mode = current_app.config.get('APP_MODE', 'single')
            user_id_to_use = None
            # Determina user_id (da decoratore o sessione se SAAS)
            if app_mode == 'saas':
                user_id_to_use = kwargs.get('api_user_id_override') # Da API key
                if not user_id_to_use and current_user.is_authenticated: user_id_to_use = current_user.id # Da sessione
                if not user_id_to_use: # Errore
                     final_payload.update({'error_code': 'INTERNAL_AUTH_ERROR', 'message': 'Utente non identificato per SAAS.'}); yield format_sse_event(final_payload, event_type='error_final'); return
                logger.info(f"SAAS Mode: User ID for Chroma: {user_id_to_use}")

            video_collection, doc_collection, article_collection = None, None, None
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            if not chroma_client: final_payload.update({'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Client ChromaDB non inizializzato.'}); yield format_sse_event(final_payload, event_type='error_final'); return

            # Ottieni le collezioni appropriate
            if app_mode == 'single':
                video_collection=current_app.config.get('CHROMA_VIDEO_COLLECTION')
                doc_collection=current_app.config.get('CHROMA_DOC_COLLECTION')
                article_collection=current_app.config.get('CHROMA_ARTICLE_COLLECTION')
            elif app_mode == 'saas':
                base_video=current_app.config.get('VIDEO_COLLECTION_NAME','video_transcripts'); base_doc=current_app.config.get('DOCUMENT_COLLECTION_NAME','document_content'); base_art=current_app.config.get('ARTICLE_COLLECTION_NAME','article_content')
                try: video_collection = chroma_client.get_collection(name=f"{base_video}_{user_id_to_use}") # Usa get_collection qui
                except: logger.warning(f"Collezione video SAAS non trovata per user {user_id_to_use}")
                try: doc_collection = chroma_client.get_collection(name=f"{base_doc}_{user_id_to_use}")
                except: logger.warning(f"Collezione documenti SAAS non trovata per user {user_id_to_use}")
                try: article_collection = chroma_client.get_collection(name=f"{base_art}_{user_id_to_use}")
                except: logger.warning(f"Collezione articoli SAAS non trovata per user {user_id_to_use}")

            if not any([video_collection, doc_collection, article_collection]):
                logger.warning(f"Nessuna collezione ChromaDB disponibile per query (Mode: {app_mode}, User: {user_id_to_use}).")

            # --- 5. Query ChromaDB ---
            yield format_sse_event({'message': 'Ricerca informazioni rilevanti...'})
            all_results_combined = []
            query_args_chroma = { 'query_embeddings': [query_embedding], 'n_results': n_results, 'include': ['documents', 'metadatas', 'distances'] }
            collections_to_query = { "VIDEO": video_collection, "DOCUMENT": doc_collection, "ARTICLE": article_collection }

            for coll_type, collection_instance in collections_to_query.items():
                 if collection_instance:
                     try:
                         logger.info(f"Querying {coll_type} collection ('{collection_instance.name}')")
                         results = collection_instance.query(**query_args_chroma)
                         docs=results.get('documents',[[]])[0]; metas=results.get('metadatas',[[]])[0]; dists=results.get('distances',[[]])[0]
                         for doc, meta, dist in zip(docs, metas, dists):
                             meta.setdefault('source_type', coll_type.lower())
                             # Assicura che 'text' sia il campo corretto per la build_prompt
                             all_results_combined.append({"text": doc, "metadata": meta, "distance": dist})
                         logger.info(f"Aggiunti {len(docs)} chunk da {coll_type}.")
                     except Exception as e_chroma_query:
                         logger.error(f"Errore query ChromaDB {coll_type} ('{collection_instance.name}'): {e_chroma_query}")

            if all_results_combined:
                 all_results_combined.sort(key=lambda x: x.get('distance', float('inf')))
                 logger.info(f"Risultati ChromaDB totali: {len(all_results_combined)} chunk.")
            else:
                logger.warning("Nessun risultato trovato da NESSUNA collezione ChromaDB disponibile.")

            # --- 6. Chiamata LLM ---
            yield format_sse_event({'message': 'Formulazione risposta...'})
            prompt = build_prompt(query_text_internal, all_results_combined)
            model_name=current_app.config.get('RAG_GENERATIVE_MODEL')
            if not model_name: raise RuntimeError("RAG_GENERATIVE_MODEL non configurato.")

            llm_answer=None; llm_success=False; block_reason_llm=None
            try:
                model=genai.GenerativeModel(model_name, safety_settings=current_app.config.get('RAG_SAFETY_SETTINGS'))
                response_llm=model.generate_content(prompt, generation_config=genai.types.GenerationConfig(**current_app.config.get('RAG_GENERATION_CONFIG',{})))
                try: llm_answer=response_llm.text; llm_success=True; logger.info("Risposta LLM generata.")
                except ValueError: block_reason_llm=getattr(getattr(response_llm,'prompt_feedback',None),'block_reason','UNKNOWN'); llm_answer=f"BLOCKED:{getattr(block_reason_llm,'name','UNKNOWN')}"; logger.warning(f"Risposta LLM bloccata: {llm_answer}") # Usa getattr per name
                except Exception as e_txt: llm_answer="Errore lettura LLM."; logger.error(f"Errore .text LLM: {e_txt}")
            except Exception as e: # Cattura errori API LLM
                error_code='LLM_GENERATION_FAILED'; message=f'Errore LLM: {e}'
                # ... (gestione errori specifici API Google come prima) ...
                if isinstance(e, google_exceptions.ResourceExhausted): error_code='API_RATE_LIMIT_GENERATION'; message='Limite API LLM.'
                elif isinstance(e, google_exceptions.GoogleAPIError): error_code='API_ERROR_GENERATION'; message=f'Errore API Google LLM ({getattr(e, "code", "N/A")}).'
                final_payload.update({'error_code': error_code, 'message': message}); yield format_sse_event(final_payload, event_type='error_final'); return

            # --- 7. Evento Risultato Finale ---
            final_payload.update({
                'success': llm_success, 'query': query_text_internal, 'answer': llm_answer,
                'retrieved_results': all_results_combined
            })
            if not llm_success and block_reason_llm: final_payload['error_code']='GENERATION_BLOCKED'; final_payload['message']=f"Risposta bloccata ({getattr(block_reason_llm,'name','UNKNOWN')})."
            elif not llm_success and not final_payload.get('error_code'): final_payload['error_code']='LLM_RESPONSE_ERROR'; final_payload['message']=llm_answer
            logger.info(f"Invio payload finale SSE: Success={final_payload['success']}")
            yield format_sse_event(final_payload, event_type='result')

        # --- Gestione Errori Generali del Generatore ---
        except Exception as e_general_sse_gen:
            logger.exception(f"Errore CRITICO non gestito nel generatore SSE per query '{query_text_for_log}': {e_general_sse_gen}")
            if not final_payload.get("error_code"):
                final_payload.update({ 'error_code': 'UNEXPECTED_SERVER_ERROR_SSE', 'message': f'Errore server: {str(e_general_sse_gen)}', 'success': False, 'answer':None, 'retrieved_results':[] })
            yield format_sse_event(final_payload, event_type='error_final')

    # --- Fine Generatore generate_events ---

    logger.info(f"Richiesta SSE per '{query_text_for_log}' pronta per lo streaming.")
    # Restituisci la Response con lo stream
    return Response(stream_with_context(generate_events()), mimetype='text/event-stream')

# --- FINE FILE app/api/routes/search.py ---