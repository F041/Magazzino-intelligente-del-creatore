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

        # --- NUOVA LOGICA JWT (priorità 1) ---
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            jwt_token = auth_header.split(" ")[1]
            logger.debug(f"Trovato header Authorization con token JWT.")
            try:
                secret_key = current_app.config.get('SECRET_KEY')
                payload = jwt.decode(jwt_token, secret_key, algorithms=["HS256"], audience='widget_user')
                
                user_id_from_jwt = payload.get('sub') # 'sub' è lo standard per l'ID utente
                user_name_from_jwt = payload.get('name', 'N/D')

                if not user_id_from_jwt:
                    raise jwt.InvalidTokenError("Token JWT non contiene user_id ('sub').")

                logger.info(f"Token JWT valido per utente '{user_name_from_jwt}' (ID associato: {user_id_from_jwt}).")
                # Passiamo l'user_id del proprietario dei dati alla funzione protetta
                kwargs['api_user_id_override'] = user_id_from_jwt
                return f(*args, **kwargs)

            except jwt.ExpiredSignatureError:
                logger.warning("Tentativo di accesso con token JWT scaduto.")
                return jsonify({"success": False, "error_code": "TOKEN_EXPIRED", "message": "Il link di accesso è scaduto."}), 401
            except jwt.InvalidTokenError as e:
                logger.warning(f"Tentativo di accesso con token JWT non valido: {e}")
                return jsonify({"success": False, "error_code": "INVALID_TOKEN", "message": "Il token di accesso non è valido."}), 401

        # --- LOGICA API KEY (priorità 2) ---
        provided_key = request.headers.get('X-API-Key')
        if provided_key:
            logger.debug(f"Trovato header X-API-Key.")
            user_id_for_api = None; key_name = None
            db_path = current_app.config.get('DATABASE_FILE'); conn = None
            try:
                # ... (la logica di controllo API KEY rimane identica a prima) ...
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

        # --- LOGICA SESSIONE BROWSER (ultima priorità) ---
        if current_user.is_authenticated:
            logger.debug("Accesso API via sessione Flask (es. chat interna).")
            return f(*args, **kwargs)

        # --- Se nessun metodo di autenticazione ha funzionato ---
        logger.warning("Accesso API negato: nessun metodo di autenticazione valido fornito (JWT, API Key, o Sessione).")
        return jsonify({"success": False, "error_code": "UNAUTHORIZED", "message": "Autenticazione richiesta."}), 401
    return decorated_function

# Funzione build_prompt (rimane invariata)
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
Il tuo compito è rispondere alla "Domanda dell'utente attuale".

Per formulare la tua risposta, considera attentamente:
1.  La "Cronologia Conversazione Precedente" per capire il contesto e a cosa si riferiscono pronomi o domande vaghe.
2.  Il "Contesto fornito dai documenti" recuperato specificamente per la domanda attuale.

**Istruzioni Fondamentali:**
-   La tua risposta deve essere basata **primariamente** sulle informazioni trovate nel "Contesto fornito dai documenti".
-   Usa la "Cronologia Conversazione Precedente" per interpretare correttamente la "Domanda dell'utente attuale", specialmente se contiene riferimenti a informazioni scambiate in precedenza.
-   Se la "Domanda dell'utente attuale" può essere risposta direttamente usando informazioni dalla "Cronologia Conversazione Precedente" (ad esempio, se chiede un chiarimento su qualcosa che hai appena detto), puoi usare quella informazione.
-   **NON aggiungere informazioni esterne o tue conoscenze generali.**
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
def handle_search_request(*args, **kwargs): # Nome più generico
    # Determina il tipo di risposta desiderato dal client
    # Il client SSE (chat.js) imposta 'Accept: text/event-stream'
    # Il bot Telegram (requests) di default imposta 'Accept: */*' o application/json se specifichiamo
    accept_header = request.headers.get('Accept', '')


    is_sse_request = 'text/event-stream' in accept_header.lower()

    logger.info(f"Richiesta di ricerca ricevuta. Accept Header: '{accept_header}', SSE Richiesto: {is_sse_request}")

    # --- Logica interna per eseguire la ricerca e generare la risposta ---
    def execute_search_logic(**kwargs):
        # Questa funzione interna conterrà la logica di ricerca che prima era nel generatore SSE.
        # Restituirà un dizionario con il payload finale o solleverà un'eccezione.
        final_payload = { "success": False, "answer": None, "retrieved_results": [], "error_code": None, "message": None }
        query_text_internal = "N/D"

        try:
            if not request.is_json:
                final_payload.update({'error_code': 'INVALID_CONTENT_TYPE', 'message': 'Richiesta deve essere JSON.'})
                raise ValueError("Richiesta non JSON") # Solleva eccezione per gestione centralizzata

            data = request.get_json()
            query_text_internal = data.get('query')
            history_from_request = data.get('history', [])

            logger.info(f"Dati richiesta: Query='{str(query_text_internal)[:100]}', History Items={len(history_from_request)}")

            if not query_text_internal or not isinstance(query_text_internal, str) or not query_text_internal.strip():
                 final_payload.update({'error_code': 'VALIDATION_ERROR', 'message': "Testo della domanda mancante o non valido."})
                 raise ValueError("Query mancante o non valida")

            default_n_results = current_app.config.get('RAG_DEFAULT_N_RESULTS', 10)
            n_results = default_n_results
            n_results_raw = data.get('n_results', default_n_results)
            try:
                n_results_temp = int(n_results_raw)
                n_results = n_results_temp if 0 < n_results_temp <= 50 else default_n_results
            except (ValueError, TypeError):
                n_results = default_n_results

            if not get_gemini_embeddings:
                 final_payload.update({'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Servizio Embedding non disponibile.'})
                 raise RuntimeError("Servizio Embedding non disponibile")

            # Generazione Embedding Query
            query_embedding = None
            try:
                llm_api_key=current_app.config.get('GOOGLE_API_KEY'); embedding_model=current_app.config.get('GEMINI_EMBEDDING_MODEL')
                if not llm_api_key or not embedding_model: raise RuntimeError("Config API Key/Model mancante.")
                query_embedding_list = get_gemini_embeddings([query_text_internal], api_key=llm_api_key, model_name=embedding_model, task_type=TASK_TYPE_QUERY)
                if not query_embedding_list or not query_embedding_list[0]: raise RuntimeError("Fallimento embedding (None o lista vuota).")
                query_embedding = query_embedding_list[0]; logger.info("Embedding query generato.")
            except Exception as e_emb:
                error_code_emb = 'EMBEDDING_UNEXPECTED_ERROR'; message_emb = f'Errore embedding: {e_emb}'
                if isinstance(e_emb, google_exceptions.ResourceExhausted): error_code_emb = 'API_RATE_LIMIT_EMBEDDING'; message_emb = 'Limite API embedding.'
                elif isinstance(e_emb, google_exceptions.GoogleAPIError): error_code_emb = 'API_ERROR_EMBEDDING'; message_emb = f'Errore API Google embedding ({getattr(e_emb, "code", "N/A")}).'
                elif isinstance(e_emb, RuntimeError): error_code_emb = 'EMBEDDING_GENERATION_FAILED'; message_emb = str(e_emb)
                logger.error(f"{error_code_emb}: {message_emb}", exc_info=True)
                final_payload.update({'error_code': error_code_emb, 'message': message_emb}); raise e_emb # Rilancia per gestione centralizzata

            # Ottenimento Collezioni ChromaDB
            app_mode = current_app.config.get('APP_MODE', 'single')
            user_id_to_use = None
            if app_mode == 'saas':
                    # 2. NUOVA LOGICA DI VERIFICA
                    user_id_to_use = kwargs.get('api_user_id_override') # Priorità 1: da JWT/API Key
                    if not user_id_to_use and current_user.is_authenticated:
                        user_id_to_use = current_user.id # Priorità 2: da sessione browser

            video_collection, doc_collection, article_collection = None, None, None
            chroma_client = current_app.config.get('CHROMA_CLIENT')
            if not chroma_client:
                final_payload.update({'error_code': 'SERVER_CONFIG_ERROR', 'message': 'Client ChromaDB non inizializzato.'});
                raise RuntimeError("Client ChromaDB non inizializzato")

            if app_mode == 'single':
                video_collection=current_app.config.get('CHROMA_VIDEO_COLLECTION')
                doc_collection=current_app.config.get('CHROMA_DOC_COLLECTION')
                article_collection=current_app.config.get('CHROMA_ARTICLE_COLLECTION')
            elif app_mode == 'saas':
                base_video=current_app.config.get('VIDEO_COLLECTION_NAME','video_transcripts'); base_doc=current_app.config.get('DOCUMENT_COLLECTION_NAME','document_content'); base_art=current_app.config.get('ARTICLE_COLLECTION_NAME','article_content')
                try: video_collection = chroma_client.get_collection(name=f"{base_video}_{user_id_to_use}")
                except Exception as e_coll_vid: logger.warning(f"Collezione video SAAS '{base_video}_{user_id_to_use}' non trovata o errore: {e_coll_vid}")
                try: doc_collection = chroma_client.get_collection(name=f"{base_doc}_{user_id_to_use}")
                except Exception as e_coll_doc: logger.warning(f"Collezione documenti SAAS '{base_doc}_{user_id_to_use}' non trovata o errore: {e_coll_doc}")
                try: article_collection = chroma_client.get_collection(name=f"{base_art}_{user_id_to_use}")
                except Exception as e_coll_art: logger.warning(f"Collezione articoli SAAS '{base_art}_{user_id_to_use}' non trovata o errore: {e_coll_art}")

            # Query ChromaDB
            all_results_combined = []
            query_args_chroma = { 'query_embeddings': [query_embedding], 'n_results': n_results, 'include': ['documents', 'metadatas', 'distances'] }
            collections_to_query = { "VIDEO": video_collection, "DOCUMENT": doc_collection, "ARTICLE": article_collection }
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
                         # Potremmo decidere di continuare o sollevare un errore qui

            if all_results_combined:
                 all_results_combined.sort(key=lambda x: x.get('distance', float('inf')))
                 logger.info(f"Risultati ChromaDB totali: {len(all_results_combined)} chunk.")
            else:
                logger.warning("Nessun risultato trovato da NESSUNA collezione ChromaDB disponibile.")

            # Chiamata LLM (con logica di fallback)
            prompt = build_prompt(query_text_internal, all_results_combined, history=history_from_request)
            models_to_try = current_app.config.get('RAG_MODELS_LIST', [])
            if not models_to_try:
                raise RuntimeError("Nessun modello RAG configurato. Controlla la variabile RAG_MODELS_LIST nel file .env.")

            llm_answer = None
            llm_success = False
            last_error = None # Memorizza l'ultimo errore se tutti i modelli falliscono

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

                    # Controlla se la risposta è stata bloccata per motivi di sicurezza
                    try:
                        llm_answer = response_llm.text
                        llm_success = True
                        logger.info(f"Risposta LLM generata con successo dal modello {model_name}.")
                        break  # Successo! Esci dal ciclo.

                    except ValueError:
                        block_reason_obj = getattr(getattr(response_llm, 'prompt_feedback', None), 'block_reason', None)
                        block_reason_name = getattr(block_reason_obj, 'name', 'UNKNOWN_REASON')
                        llm_answer = f"BLOCKED:{block_reason_name}"
                        llm_success = False
                        logger.warning(f"Risposta LLM bloccata dal modello {model_name} per motivo: {block_reason_name}. Tento con il prossimo modello.")
                        last_error = ValueError(f"Blocked by model {model_name}") # Memorizziamo l'errore per sicurezza
                        continue 
                    
                except (google_exceptions.NotFound, google_exceptions.PermissionDenied, google_exceptions.InternalServerError) as e_fallback:
                    last_error = e_fallback
                    logger.warning(f"Modello '{model_name}' non trovato o non accessibile. Tento con il prossimo modello nella lista.")
                    continue # Prova il prossimo modello
                
                except Exception as e_llm_gen:
                    # Per altri errori (es. rate limit), usciamo subito senza provare altri modelli
                    logger.error(f"Errore critico durante la generazione con il modello '{model_name}': {e_llm_gen}", exc_info=True)
                    last_error = e_llm_gen
                    llm_success = False
                    break # Esci dal ciclo e gestisci l'errore

            # Se siamo usciti dal ciclo senza successo e con un errore, lo gestiamo qui
            if not llm_success and last_error:
                error_code_llm = 'LLM_GENERATION_FAILED'; message_llm = f'Errore LLM dopo aver provato i modelli disponibili: {last_error}'
                if isinstance(last_error, (google_exceptions.NotFound, google_exceptions.PermissionDenied)):
                    error_code_llm = 'LLM_MODEL_NOT_AVAILABLE'
                    message_llm = 'Nessuno dei modelli configurati è risultato accessibile o disponibile.'
                elif isinstance(last_error, google_exceptions.GoogleAPIError):
                    error_code_llm = 'API_ERROR_GENERATION'; message_llm = f'Errore API Google LLM ({getattr(last_error, "code", "N/A")}).'
                
                logger.error(f"{error_code_llm}: {message_llm}")
                final_payload.update({'error_code': error_code_llm, 'message': message_llm});
                raise last_error

            # Costruzione Payload Finale
            final_payload.update({
                'success': llm_success, 'query': query_text_internal, 'answer': llm_answer,
                'retrieved_results': all_results_combined
            })
            if not llm_success and llm_answer and llm_answer.startswith("BLOCKED:"):
                final_payload['error_code']='GENERATION_BLOCKED'
                final_payload['message']=f"Risposta bloccata ({llm_answer.split(':',1)[1]})."
            elif not llm_success and not final_payload.get('error_code'):
                final_payload['error_code']='LLM_RESPONSE_ERROR'
                final_payload['message']=llm_answer or "Errore sconosciuto risposta LLM."

            return final_payload

        # --- Gestione errori centralizzata per execute_search_logic ---
        except (ValueError, RuntimeError, google_exceptions.GoogleAPIError, Exception) as e_logic:
            # Se final_payload non è stato aggiornato con un errore specifico, mettine uno generico
            if not final_payload.get("error_code"):
                final_payload['error_code'] = 'INTERNAL_PROCESSING_ERROR'
                final_payload['message'] = f"Errore interno durante l'elaborazione della ricerca: {str(e_logic)}"
            final_payload['success'] = False # Assicura che success sia false
            # Logga l'errore se non è già stato loggato in modo specifico prima
            if not isinstance(e_logic, (google_exceptions.ResourceExhausted, google_exceptions.GoogleAPIError)):
                 logger.exception(f"Errore in execute_search_logic per query '{query_text_internal}': {e_logic}")
            return final_payload # Restituisce il payload con l'errore

    # --- Logica per decidere il tipo di risposta ---
    if is_sse_request:
        # Comportamento SSE (come prima, ma ora chiama execute_search_logic per il payload)
        def generate_events_sse():
            # Invia messaggi di stato iniziali SSE
            yield format_sse_event({'message': 'Analisi domanda...'})
            yield format_sse_event({'message': 'Creazione rappresentazione semantica...'})
            yield format_sse_event({'message': 'Accesso base di conoscenza...'})
            yield format_sse_event({'message': 'Ricerca informazioni rilevanti...'})
            yield format_sse_event({'message': 'Formulazione risposta...'})

            # Esegui la logica di ricerca
            search_result_payload = execute_search_logic()

            # Invia l'evento finale 'result' o 'error_final'
            event_type_final = 'result' if search_result_payload.get('success') else 'error_final'
            logger.info(f"Invio payload finale SSE: Success={search_result_payload.get('success')}, Evento: {event_type_final}")
            yield format_sse_event(search_result_payload, event_type=event_type_final)

        logger.info(f"Rispondo con stream SSE per query '{str(request.get_json().get('query'))[:100] if request.is_json else 'N/A'}'.")
        return Response(stream_with_context(generate_events_sse()), mimetype='text/event-stream')
    else:
        # Comportamento JSON singolo (per il bot)
        logger.info(f"Rispondo con JSON singolo per query '{str(request.get_json().get('query'))[:100] if request.is_json else 'N/A'}'.")

        search_result_payload = execute_search_logic() # Esegui la logica di ricerca

        # Determina lo status code HTTP basato sul successo
        status_code = 200 if search_result_payload.get('success') else 500 # Potrebbe essere più granulare (es. 400 per validation error)
        if search_result_payload.get('error_code') == 'VALIDATION_ERROR': status_code = 400
        if search_result_payload.get('error_code') == 'UNAUTHORIZED': status_code = 401 # Anche se gestito da @require_api_key

        return jsonify(search_result_payload), status_code
