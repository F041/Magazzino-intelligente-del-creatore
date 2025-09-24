import logging
import sqlite3
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, jsonify
from flask_login import login_required, current_user
import requests

logger = logging.getLogger(__name__)
settings_bp = Blueprint('settings', __name__)

@settings_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    user_id = current_user.id
    db_path = current_app.config.get('DATABASE_FILE')
    
    if request.method == 'POST':
        provider = request.form.get('llm_provider')
        
         # 1. Gestione del MODELLO DI GENERAZIONE
        combined_models = ""
        if provider == 'google' or provider == 'groq':
            primary_model = request.form.get('llm_model_name_primary', '').strip()
            fallback_model = request.form.get('llm_model_name_fallback', '').strip()
            model_list = [model for model in [primary_model, fallback_model] if model]
            combined_models = ",".join(model_list)
        elif provider == 'ollama':
            combined_models = request.form.get('ollama_model_name', '').strip()

        # 2. Gestione del MODELLO DI EMBEDDING (LA CORREZIONE È QUI)
        embedding_model_to_save = ""
        if provider == 'ollama':
            embedding_model_to_save = request.form.get('ollama_embedding_model')
        else: # Per Google e Groq, che usano lo stesso campo
            embedding_model_to_save = request.form.get('llm_embedding_model')

        # 3. Raccolta di tutti i dati da salvare
        settings_to_save = {
            'llm_provider': provider,
            'llm_model_name': combined_models,
            'llm_embedding_model': embedding_model_to_save,
            'llm_api_key': request.form.get('llm_api_key'),
            'ollama_base_url': request.form.get('ollama_base_url'),
            'wordpress_url': request.form.get('wordpress_url'),
            'wordpress_username': request.form.get('wordpress_username'),
            'wordpress_api_key': request.form.get('wordpress_api_key')
        }
        
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            # La query SQL rimane identica, perché i dati sono già stati preparati correttamente
            cursor.execute("""
                INSERT INTO user_settings (user_id, llm_provider, llm_model_name, llm_embedding_model, llm_api_key, ollama_base_url, wordpress_url, wordpress_username, wordpress_api_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    llm_provider = excluded.llm_provider,
                    llm_model_name = excluded.llm_model_name,
                    llm_embedding_model = excluded.llm_embedding_model,
                    llm_api_key = excluded.llm_api_key,
                    ollama_base_url = excluded.ollama_base_url,
                    wordpress_url = excluded.wordpress_url,
                    wordpress_username = excluded.wordpress_username,
                    wordpress_api_key = excluded.wordpress_api_key;
            """, (user_id, 
                  settings_to_save['llm_provider'], 
                  settings_to_save['llm_model_name'], 
                  settings_to_save['llm_embedding_model'], 
                  settings_to_save['llm_api_key'],
                  settings_to_save['ollama_base_url'],
                  settings_to_save['wordpress_url'],
                  settings_to_save['wordpress_username'],
                  settings_to_save['wordpress_api_key']))
            conn.commit()
            flash('Impostazioni salvate con successo!', 'success')
        except sqlite3.Error as e:
            logger.error(f"Errore DB salvando le impostazioni per l'utente {user_id}: {e}")
            flash('Errore durante il salvataggio delle impostazioni.', 'error')
        finally:
            if conn:
                conn.close()
        
        return redirect(url_for('settings.settings_page'))

    # Il resto della funzione per il metodo GET rimane quasi uguale, ma lo semplifichiamo
    user_settings = {}
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
        settings_row = cursor.fetchone()
        if settings_row:
            user_settings = dict(settings_row)
            # Garantiamo il valore di default per il provider se in DB è NULL/None
            user_settings['llm_provider'] = user_settings.get('llm_provider') or 'google'
            # Logica per splittare i modelli se il provider è Google
            if user_settings.get('llm_provider') == 'google':
                combined_models = user_settings.get('llm_model_name', '') or ''
                models_parts = [model.strip() for model in combined_models.split(',') if model.strip()]
                user_settings['llm_model_name_primary'] = models_parts[0] if len(models_parts) > 0 else ''
                user_settings['llm_model_name_fallback'] = models_parts[1] if len(models_parts) > 1 else ''
        else:
            # Nessuna riga per l'utente: garantiamo valori di default espliciti
            user_settings = {
                'llm_provider': 'google',
                'llm_model_name_primary': '',
                'llm_model_name_fallback': '',
                'llm_embedding_model': '',
                'llm_api_key': '',
                'ollama_base_url': 'http://localhost:11434',
                'llm_model_name': ''
            }
    except sqlite3.Error as e:
        logger.error(f"Errore DB caricando le impostazioni per l'utente {user_id}: {e}")
        flash('Errore nel caricamento delle impostazioni.', 'error')
    finally:
        if conn:
            conn.close()
            
    return render_template('settings.html', settings=user_settings)

@settings_bp.route('/api/settings/test_ollama', methods=['POST'])
@login_required
def test_ollama_connection():
    """
    Testa la connessione a un server Ollama.
    """
    if not request.is_json:
        return jsonify({'success': False, 'message': 'Richiesta non valida.'}), 400

    data = request.get_json()
    base_url = data.get('ollama_url')
    model_name = data.get('model_name')

    if not base_url or not model_name:
        return jsonify({'success': False, 'message': 'URL e nome del modello sono richiesti.'}), 400

    api_url = base_url.rstrip('/') + "/api/generate"
    payload = {
        "model": model_name,
        "prompt": "Rispondi solo con la parola 'test'",
        "stream": False
    }

    logger.info(f"Test connessione Ollama: URL={api_url}, Modello={model_name}")

    try:
        # Usiamo un timeout breve per il test di connessione
        response = requests.post(api_url, json=payload, timeout=20)
        
        # Controlla se Ollama ha risposto con un errore specifico (es. modello non trovato)
        if response.status_code == 404:
            raise requests.exceptions.RequestException(f"Modello '{model_name}' non trovato sul server Ollama. Hai eseguito 'ollama pull {model_name}'?")

        response.raise_for_status() # Controlla altri errori HTTP (es. 500)
        
        return jsonify({'success': True, 'message': f"Connessione con il modello '{model_name}' riuscita!"})

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout durante il test di connessione a Ollama: {base_url}")
        return jsonify({'success': False, 'message': f"Timeout: impossibile raggiungere Ollama a '{base_url}'. Controlla l'indirizzo e che non ci sia un firewall."}), 408
    except requests.exceptions.RequestException as e:
        logger.warning(f"Errore di connessione durante il test di Ollama: {e}")
        return jsonify({'success': False, 'message': f"Errore di connessione: {e}"}), 500
    except Exception as e:
        logger.error(f"Errore generico test Ollama: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f"Errore imprevisto: {e}"}), 500
    
@settings_bp.route('/api/settings/reset_ai', methods=['POST'])
@login_required
def reset_ai_settings():
    """
    Ripristina le impostazioni AI di un utente ai valori predefiniti
    eliminando le sue configurazioni personalizzate dal database.
    """
    user_id = current_user.id
    db_path = current_app.config.get('DATABASE_FILE')
    logger.info(f"Richiesta di ripristino impostazioni AI per l'utente: {user_id}")
    
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Per ripristinare, basta cancellare le colonne specifiche. 
        # Un approccio ancora più semplice è cancellare l'intera riga, 
        # ma questo cancellerebbe anche le impostazioni di WordPress.
        # Aggiorniamo solo i campi AI a NULL.
        cursor.execute("""
            UPDATE user_settings
            SET llm_provider = 'google',
                llm_model_name = NULL,
                llm_embedding_model = NULL,
                llm_api_key = NULL,
                ollama_base_url = NULL
            WHERE user_id = ?
        """, (user_id,))

        conn.commit()
        
        return jsonify({'success': True, 'message': 'Impostazioni AI ripristinate ai valori predefiniti.'})

    except sqlite3.Error as e:
        if conn: conn.rollback()
        logger.error(f"Errore DB durante il ripristino delle impostazioni per l'utente {user_id}: {e}")
        return jsonify({'success': False, 'message': 'Errore del database durante il ripristino.'}), 500
    finally:
        if conn: conn.close()