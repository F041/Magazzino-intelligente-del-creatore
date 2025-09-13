import logging
from flask import Blueprint, request, jsonify, current_app, Response, stream_with_context
from flask_login import current_user
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import List, Dict, Optional
from functools import wraps
import sqlite3
import json
import time
import jwt
import cohere

try:
    from app.services.embedding.gemini_embedding import get_gemini_embeddings, TASK_TYPE_QUERY
except ImportError:
    logger = logging.getLogger(__name__)
    logger.error("!!! Impossibile importare get_gemini_embeddings (search.py) !!!")
    get_gemini_embeddings = None
    TASK_TYPE_QUERY = "retrieval_query"

logger = logging.getLogger(__name__)
search_bp = Blueprint('search', __name__)

# Funzione Helper SSE
def format_sse_event(data_dict: dict, event_type: str = 'status') -> str:
    json_data = json.dumps(data_dict)
    return f"event: {event_type}\ndata: {json_data}\n\n"

# Decoratore @require_api_key
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        app_mode = current_app.config.get('APP_MODE', 'single')
        if app_mode == 'single':
            logger.debug("Decoratore @require_api_key: Modalità SINGLE. Accesso consentito.")
            return f(*args, **kwargs)

        logger.debug("Decoratore @require_api_key: Modalità SAAS. Controllo autenticazione...")

        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            jwt_token = auth_header.split(" ")[1]
            logger.debug(f"Trovato header Authorization con token JWT.")
            try:
                secret_key = current_app.config.get('SECRET_KEY')
                payload = jwt.decode(jwt_token, secret_key, algorithms=["HS256"], audience='widget_user')
                
                user_id_from_jwt = payload.get('sub')
                if not user_id_from_jwt:
                    raise jwt.InvalidTokenError("Token JWT non contiene user_id ('sub').")

                logger.info(f"Token JWT valido per utente associato a ID: {user_id_from_jwt}.")
                kwargs['api_user_id_override'] = user_id_from_jwt
                return f(*args, **kwargs)

            except jwt.ExpiredSignatureError:
                logger.warning("Tentativo di accesso con token JWT scaduto.")
                return jsonify({"success": False, "error_code": "TOKEN_EXPIRED", "message": "Il link di accesso è scaduto."}), 401
            except jwt.InvalidTokenError as e:
                logger.warning(f"Tentativo di accesso con token JWT non valido: {e}")
                return jsonify({"success": False, "error_code": "INVALID_TOKEN", "message": "Il token di accesso non è valido."}), 401

        provided_key = request.headers.get('X-API-Key')
        if provided_key:
            logger.debug(f"Trovato header X-API-Key.")
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
                    except: conn.rollback()
                else:
                    logger.warning(f"API Key ('{provided_key[:5]}...') non valida/attiva."); return jsonify({"success": False, "error_code": "UNAUTHORIZED", "message": "Chiave API non valida o revocata."}), 401
            except Exception as db_err: logger.error(f"Errore DB validazione API Key: {db_err}"); return jsonify({"success": False, "error_code": "DB_ERROR", "message": "Errore database validazione chiave."}), 500
            finally:
                if conn: conn.close()
            if user_id_for_api: kwargs['api_user_id_override'] = user_id_for_api; return f(*args, **kwargs)
            else: return jsonify({"success": False, "error_code": "INTERNAL_SERVER_ERROR", "message": "Errore determinazione utente da chiave API."}), 500

        if current_user.is_authenticated:
            logger.debug("Accesso API via sessione Flask (es. chat interna).")
            return f(*args, **kwargs)

        logger.warning("Accesso API negato: nessun metodo di autenticazione valido fornito (JWT, API Key, o Sessione).")
        return jsonify({"success": False, "error_code": "UNAUTHORIZED", "message": "Autenticazione richiesta."}), 401
    return decorated_function

# Funzione build_prompt (con la tua modifica)
def build_prompt(query: str, context_chunks: List[Dict], history: Optional[List[Dict]] = None) -> str:
    context_text = "\n---\n".join([chunk.get('text', '') for chunk in context_chunks])
    history_str = ""
    if history:
        logger.info(f"build_prompt: Ricevuta cronologia con {len(history)} messaggi.")
        for i, msg in enumerate(history):
            role = "Utente" if msg.get("role") == "user" else "Assistente"
            content = msg.get("content", "")
            history_str += f"{role}: {content}\n"
    else:
        logger.info("build_prompt: Nessuna cronologia ricevuta.")

    if not context_chunks:
        prompt = f"""Sei l'assistente AI del "Magazzino del creatore".
La conversazione precedente (se presente):
{history_str if history_str else "Nessuna."}
Rispondi brevemente alla seguente domanda basandoti sulle tue conoscenze generali e sulla conversazione precedente,
ma specifica che non hai trovato contesto specifico nei documenti analizzati per QUESTA domanda.

Domanda Attuale: {query}

Risposta (senza contesto specifico per questa domanda):"""
        logger.warning("Nessun chunk di contesto recuperato per la query. Uso prompt generico con eventuale cronologia.")
        return prompt

    prompt = f"""Sei l'assistente AI del progetto "Magazzino del creatore".
Il tuo compito è rispondere alla "Domanda dell'utente attuale" nella lingua in cui la chiede.

Per formulare la tua risposta, considera attentamente:
1.  La "Cronologia Conversazione Precedente" per capire il contesto e a cosa si riferiscono pronomi o domande vaghe.
2.  Il "Contesto fornito dai documenti" recuperato specificamente per la domanda attuale.

**Istruzioni Fondamentali:**
-   La tua risposta deve essere basata **primariamente** sulle informazioni trovate nel "Contesto fornito dai documenti".
-   Usa la "Cronologia Conversazione Precedente" per interpretare correttamente la "Domanda dell'utente attuale", specialmente se contiene riferimenti a informazioni scambiate in precedenza.
-   Se la "Domanda dell'utente attuale" può essere risposta direttamente usando informazioni dalla "Cronologia Conversazione Precedente" (ad esempio, se chiede un chiarimento su qualcosa che hai appena detto), puoi usare quella informazione.
-   **NON aggiungere informazioni esterne o tue conoscenze generali.**
-   **Se il contesto fornito contiene una menzione diretta o una citazione che risponde alla domanda, anche se breve, riportala fedelmente. È meglio dare un'informazione piccola ma precisa piuttosto che nessuna.**
-   Se, dopo aver considerato sia la cronologia sia il contesto fornito dai documenti, non trovi una risposta diretta e fattuale alla "Domanda dell'utente attuale", rispondi ESATTAMENTE con: "Le informazioni disponibili (incluse quelle della conversazione precedente e dei documenti recuperati) non contengono una risposta diretta a questa specifica domanda." Non inventare e non aggiungere altro in questo caso.

**Cronologia Conversazione Precedente:**
{history_str if history_str else "Nessuna conversazione precedente."}
---
**Contesto fornito dai documenti (per la domanda attuale):**
---
{context_text if context_text else "Nessun contesto specifico recuperato dai documenti per questa domanda."}
---

**Domanda dell'utente attuale:** {query}

**Risposta:**"""
    return prompt

@search_bp.route('/', methods=['POST'])
@require_api_key
def handle_search_request(*args, **kwargs):
    accept_header = request.headers.get('Accept', '')
    is_sse_request = 'text/event-stream' in accept_header.lower()
    logger.info(f"Richiesta di ricerca ricevuta. Accept Header: '{accept_header}', SSE Richiesto: {is_sse_request}")

    def execute_search_logic(**kwargs):
        final_payload = { "success": False, "answer": None, "retrieved_results": [], "error_code": None, "message": None }
        query_text_internal = "N/D"

        try:
            if not request.is_json:
                final_payload.update({'error_code': 'INVALID_CONTENT_TYPE', 'message': 'Richiesta deve essere JSON.'})
                raise ValueError("Richiesta non JSON")

            data = request.get_json()
            query_text_internal = data.get('query')
            history_from_request = data.get('history', [])

            logger.info(f"Dati richiesta: Query='{str(query_text_internal)[:100]}', History Items={len(history_from_request)}")

            if not query_text_internal or not isinstance(query_text_internal, str) or not query_text_internal.strip():
                 final_payload.update({'error_code': 'VALIDATION_ERROR', 'message': "Testo della domanda mancante o non valido."})
                 raise ValueError("Query mancante o non valida")

            default_n_results = current_app.config.get('RAG_DEFAULT_N_RESULTS', 10)
            n_results = default_n_results
            try:
                n_results_temp = int(data.get('n_results', default_n_results))
                n_results = n_results_temp if 0 < n_results_temp <= 50 else default_n_results
            except (ValueError, TypeError):
                n_results = default_n_results

            if not get_gemini_embeddings:
                 final_payload.update({'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Servizio Embedding non disponibile.'})
                 raise RuntimeError("Servizio Embedding non disponibile")

            app_mode = current_app.config.get('APP_MODE', 'single')
            user_id_to_use = None
            if app_mode == 'saas':
                user_id_to_use = kwargs.get('api_user_id_override')
                if not user_id_to_use and current_user.is_authenticated:
                    user_id_to_use = current_user.id
            logger.info(f"ID utente identificato per la ricerca: {user_id_to_use}")

            llm_api_key = current_app.config.get('GOOGLE_API_KEY')
            embedding_model = current_app.config.get('GEMINI_EMBEDDING_MODEL')
            models_to_try = current_app.config.get('RAG_MODELS_LIST', [])

            if user_id_to_use:
                db_path = current_app.config.get('DATABASE_FILE')
                conn_settings = None
                try:
                    conn_settings = sqlite3.connect(db_path)
                    conn_settings.row_factory = sqlite3.Row
                    cursor_settings = conn_settings.cursor()
                    cursor_settings.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id_to_use,))
                    user_settings = cursor_settings.fetchone()
                    
                    if user_settings:
                        logger.info(f"Trovate impostazioni personalizzate per l'utente {user_id_to_use}.")
                        if user_settings['llm_api_key']:
                            llm_api_key = user_settings['llm_api_key']
                            logger.info("Uso della chiave API personalizzata dell'utente.")
                        if user_settings['llm_embedding_model']:
                            embedding_model = user_settings['llm_embedding_model']
                        if user_settings['llm_model_name']:
                            user_models = [m.strip() for m in user_settings['llm_model_name'].split(',') if m.strip()]
                            if user_models:
                                models_to_try = user_models
                                logger.info(f"Uso della lista di modelli personalizzata dell'utente: {models_to_try}")
                except sqlite3.Error as e:
                    logger.error(f"Errore DB nel recuperare le impostazioni per l'utente {user_id_to_use}: {e}")
                finally:
                    if conn_settings:
                        conn_settings.close()

            query_embedding = None
            try:
                if not llm_api_key or not embedding_model: raise RuntimeError("Config API Key/Model mancante dopo l'override.")
                query_embedding_list = get_gemini_embeddings([query_text_internal], api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_QUERY)
                if not query_embedding_list or not query_embedding_list[0]: raise RuntimeError("Fallimento embedding (None o lista vuota).")
                query_embedding = query_embedding_list[0]; logger.info("Embedding query generato.")
            except Exception as e_emb:
                error_code_emb = 'EMBEDDING_UNEXPECTED_ERROR'; message_emb = f'Errore embedding: {e_emb}'
                if isinstance(e_emb, google_exceptions.ResourceExhausted): error_code_emb = 'API_RATE_LIMIT_EMBEDDING'; message_emb = 'Limite API embedding.'
                elif isinstance(e_emb, google_exceptions.GoogleAPIError): error_code_emb = 'API_ERROR_EMBEDDING'; message_emb = f'Errore API Google embedding ({getattr(e_emb, "code", "N/A")}).'
                elif isinstance(e_emb, RuntimeError): error_code_emb = 'EMBEDDING_GENERATION_FAILED'; message_emb = str(e_emb)
                logger.error(f"{error_code_emb}: {message_emb}", exc_info=True)
                final_payload.update({'error_code': error_code_emb, 'message': message_emb}); raise e_emb

            video_collection, doc_collection, article_collection, page_collection = None, None, None, None
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            if not chroma_client:
                raise RuntimeError("Client ChromaDB non inizializzato")

            # Nomi base delle collezioni
            base_video = current_app.config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
            base_doc = current_app.config.get('DOCUMENT_COLLECTION_NAME', 'document_content')
            base_art = current_app.config.get('ARTICLE_COLLECTION_NAME', 'article_content')
            base_page = "page_content"

            if app_mode == 'single':
                try: video_collection = chroma_client.get_collection(name=base_video)
                except Exception: logger.warning(f"Collezione '{base_video}' non trovata.")
                try: doc_collection = chroma_client.get_collection(name=base_doc)
                except Exception: logger.warning(f"Collezione '{base_doc}' non trovata.")
                try: article_collection = chroma_client.get_collection(name=base_art)
                except Exception: logger.warning(f"Collezione '{base_art}' non trovata.")
                try: page_collection = chroma_client.get_collection(name=base_page)
                except Exception: logger.warning(f"Collezione '{base_page}' non trovata.")
            
            elif app_mode == 'saas':
                if user_id_to_use:
                    try: video_collection = chroma_client.get_collection(name=f"{base_video}_{user_id_to_use}")
                    except Exception: logger.warning(f"Collezione video per utente {user_id_to_use} non trovata.")
                    try: doc_collection = chroma_client.get_collection(name=f"{base_doc}_{user_id_to_use}")
                    except Exception: logger.warning(f"Collezione documenti per utente {user_id_to_use} non trovata.")
                    try: article_collection = chroma_client.get_collection(name=f"{base_art}_{user_id_to_use}")
                    except Exception: logger.warning(f"Collezione articoli per utente {user_id_to_use} non trovata.")
                    try: page_collection = chroma_client.get_collection(name=f"{base_page}_{user_id_to_use}")
                    except Exception: logger.warning(f"Collezione pagine per utente {user_id_to_use} non trovata.")

            all_results_combined = []
            query_args_chroma = { 'query_embeddings': [query_embedding], 'n_results': n_results, 'include': ['documents', 'metadatas', 'distances'] }
            collections_to_query = {
                "VIDEO": video_collection, 
                "DOCUMENT": doc_collection, 
                "ARTICLE": article_collection,
                "PAGE": page_collection 
            }
            for coll_type, collection_instance in collections_to_query.items():
                 if collection_instance:
                     try:
                         logger.info(f"Querying {coll_type} collection ('{collection_instance.name}')")
                         results = collection_instance.query(**query_args_chroma)
                         docs=results.get('documents',[[]])[0]; metas=results.get('metadatas',[[]])[0]; dists=results.get('distances',[[]])[0]
                         for doc_text, meta, dist in zip(docs, metas, dists):
                             meta.setdefault('source_type', coll_type.lower())
                             all_results_combined.append({"text": doc_text, "metadata": meta, "distance": dist})
                         logger.info(f"Aggiunti {len(docs)} chunk da {coll_type}.")
                     except Exception as e_chroma_query:
                         logger.error(f"Errore query ChromaDB {coll_type} ('{collection_instance.name}'): {e_chroma_query}", exc_info=True)

            chunks_for_prompt = []
            if all_results_combined:
                all_results_combined.sort(key=lambda x: x.get('distance', float('inf')))
                logger.info(f"Recuperati {len(all_results_combined)} chunk iniziali da ChromaDB.")

                cohere_api_key = current_app.config.get('COHERE_API_KEY')
                if cohere_api_key:
                    try:
                        logger.info("Avvio re-ranking con l'API di Cohere...")
                        co = cohere.Client(cohere_api_key)
                        docs_to_rerank = [chunk['text'] for chunk in all_results_combined]
                        
                        rerank_results = co.rerank(
                            query=query_text_internal,
                            documents=docs_to_rerank,
                            top_n=15, # da 7
                            model='rerank-multilingual-v3.0'
                        )
                        
                        reranked_chunks = []
                        for hit in rerank_results.results:
                            original_chunk = all_results_combined[hit.index]
                            original_chunk['rerank_score'] = hit.relevance_score
                            reranked_chunks.append(original_chunk)
                        
                        logger.info(f"Re-ranking completato. Selezionati i migliori {len(reranked_chunks)} chunk.")
                        chunks_for_prompt = reranked_chunks
                    except Exception as e:
                        logger.error(f"Errore durante il re-ranking con Cohere: {e}. Uso i risultati originali.")
                        chunks_for_prompt = all_results_combined[:15]
                else:
                    logger.warning("COHERE_API_KEY non configurata. Salto il re-ranking e prendo i primi 7 risultati.")
                    chunks_for_prompt = all_results_combined[:15]
            else:
                logger.warning("Nessun risultato trovato da ChromaDB.")
                chunks_for_prompt = []

            prompt = build_prompt(query_text_internal, chunks_for_prompt, history=history_from_request)
            
            if not models_to_try:
                raise RuntimeError("Nessun modello RAG configurato. Controlla le impostazioni o il file .env.")

            llm_answer = None
            llm_success = False
            last_error = None

            for model_name in models_to_try:
                logger.info(f"Tentativo di generazione risposta con il modello: {model_name}")
                try:
                    model = genai.GenerativeModel(
                        model_name,
                        safety_settings=current_app.config.get('RAG_SAFETY_SETTINGS', {})
                    )
                    response_llm = model.generate_content(
                        prompt,
                        generation_config=genai.types.GenerationConfig(**current_app.config.get('RAG_GENERATION_CONFIG', {}))
                    )
                    try:
                        llm_answer = response_llm.text
                        llm_success = True
                        logger.info(f"Risposta LLM generata con successo dal modello {model_name}.")
                        break
                    except ValueError:
                        block_reason_obj = getattr(getattr(response_llm, 'prompt_feedback', None), 'block_reason', None)
                        block_reason_name = getattr(block_reason_obj, 'name', 'UNKNOWN_REASON')
                        llm_answer = f"BLOCKED:{block_reason_name}"
                        llm_success = False
                        logger.warning(f"Risposta LLM bloccata dal modello {model_name} per motivo: {block_reason_name}. Tento con il prossimo modello.")
                        last_error = ValueError(f"Blocked by model {model_name}")
                        continue
                except (google_exceptions.NotFound, google_exceptions.PermissionDenied, google_exceptions.InternalServerError, google_exceptions.ResourceExhausted) as e_fallback: # <-- MODIFICA QUI
                    last_error = e_fallback
                    logger.warning(f"Modello '{model_name}' non trovato, non accessibile o rate-limited. Tento con il prossimo modello nella lista.")
                    continue
                except Exception as e_llm_gen:
                    logger.error(f"Errore critico durante la generazione con il modello '{model_name}': {e_llm_gen}", exc_info=True)
                    last_error = e_llm_gen
                    llm_success = False
                    break

            if not llm_success and last_error:
                error_code_llm = 'LLM_GENERATION_FAILED'; message_llm = f'Errore LLM dopo aver provato i modelli disponibili: {last_error}'
                if isinstance(last_error, (google_exceptions.NotFound, google_exceptions.PermissionDenied)):
                    error_code_llm = 'LLM_MODEL_NOT_AVAILABLE'
                    message_llm = 'Nessuno dei modelli configurati è risultato accessibile o disponibile.'
                elif isinstance(last_error, google_exceptions.GoogleAPIError):
                    # Gestiamo il caso 429 in modo specifico nel messaggio
                    status_code = getattr(last_error, "code", 0)
                    error_message_from_api = str(last_error)
                    if status_code == 429 or "429" in error_message_from_api:
                        error_code_llm = 'API_RATE_LIMIT_EXCEEDED'
                        message_llm = 'Limite di richieste API di Google raggiunto per tutti i modelli.'
                    else:
                        error_code_llm = 'API_ERROR_GENERATION'; message_llm = f'Errore API Google LLM ({status_code}).'
                logger.error(f"{error_code_llm}: {message_llm}")
                final_payload.update({'error_code': error_code_llm, 'message': message_llm});
                raise last_error

            final_payload.update({
                'success': llm_success, 'query': query_text_internal, 'answer': llm_answer,
                'retrieved_results': chunks_for_prompt # MODIFICATO: restituiamo i chunk ri-classificati
            })
            if not llm_success and llm_answer and llm_answer.startswith("BLOCKED:"):
                final_payload['error_code']='GENERATION_BLOCKED'
                final_payload['message']=f"Risposta bloccata ({llm_answer.split(':',1)[1]})."
            elif not llm_success and not final_payload.get('error_code'):
                final_payload['error_code']='LLM_RESPONSE_ERROR'
                final_payload['message']=llm_answer or "Errore sconosciuto risposta LLM."

            return final_payload

        except (ValueError, RuntimeError, google_exceptions.GoogleAPIError, Exception) as e_logic:
            if not final_payload.get("error_code"):
                final_payload['error_code'] = 'INTERNAL_PROCESSING_ERROR'
                final_payload['message'] = f"Errore interno durante l'elaborazione della ricerca: {str(e_logic)}"
            final_payload['success'] = False
            if not isinstance(e_logic, (google_exceptions.ResourceExhausted, google_exceptions.GoogleAPIError)):
                 logger.exception(f"Errore in execute_search_logic per query '{query_text_internal}': {e_logic}")
            return final_payload

    if is_sse_request:
        def generate_events_sse():
            yield format_sse_event({'message': 'Analisi domanda...'})
            yield format_sse_event({'message': 'Creazione rappresentazione semantica...'})
            yield format_sse_event({'message': 'Accesso base di conoscenza...'})
            yield format_sse_event({'message': 'Ricerca informazioni rilevanti...'})
            yield format_sse_event({'message': 'Formulazione risposta...'})
            search_result_payload = execute_search_logic(**kwargs)
            event_type_final = 'result' if search_result_payload.get('success') else 'error_final'
            logger.info(f"Invio payload finale SSE: Success={search_result_payload.get('success')}, Evento: {event_type_final}")
            yield format_sse_event(search_result_payload, event_type=event_type_final)
        return Response(stream_with_context(generate_events_sse()), mimetype='text/event-stream')
    else:
        search_result_payload = execute_search_logic(**kwargs)
        status_code = 200 if search_result_payload.get('success') else 500
        if search_result_payload.get('error_code') == 'VALIDATION_ERROR': status_code = 400
        if search_result_payload.get('error_code') == 'UNAUTHORIZED': status_code = 401
        return jsonify(search_result_payload), status_code