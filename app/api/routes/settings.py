# FILE: app/api/routes/settings.py
import logging
import sqlite3
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app
from flask_login import login_required, current_user

logger = logging.getLogger(__name__)
settings_bp = Blueprint('settings', __name__)

@settings_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    user_id = current_user.id
    db_path = current_app.config.get('DATABASE_FILE')
    
    if request.method == 'POST':
        primary_model = request.form.get('llm_model_name_primary', '').strip()
        fallback_model = request.form.get('llm_model_name_fallback', '').strip()
        
        model_list = [model for model in [primary_model, fallback_model] if model]
        combined_models = ",".join(model_list)

        settings_to_save = {
            'llm_provider': request.form.get('llm_provider'),
            'llm_model_name': combined_models, # Salviamo la stringa unita
            'llm_embedding_model': request.form.get('llm_embedding_model'),
            'llm_api_key': request.form.get('llm_api_key')
        }
        
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            # Usiamo UPSERT per inserire se non esiste, o aggiornare se esiste
            cursor.execute("""
                INSERT INTO user_settings (user_id, llm_provider, llm_model_name, llm_embedding_model, llm_api_key)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    llm_provider = excluded.llm_provider,
                    llm_model_name = excluded.llm_model_name,
                    llm_embedding_model = excluded.llm_embedding_model,
                    llm_api_key = excluded.llm_api_key;
            """, (user_id, 
                  settings_to_save['llm_provider'], 
                  settings_to_save['llm_model_name'], 
                  settings_to_save['llm_embedding_model'], 
                  settings_to_save['llm_api_key']))
            conn.commit()
            flash('Impostazioni salvate con successo!', 'success')
        except sqlite3.Error as e:
            logger.error(f"Errore DB salvando le impostazioni per l'utente {user_id}: {e}")
            flash('Errore durante il salvataggio delle impostazioni.', 'error')
        finally:
            if conn:
                conn.close()
        
        return redirect(url_for('settings.settings_page'))

    # Logica per caricare le impostazioni esistenti (metodo GET)
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
            # Separiamo la stringa dei modelli in due variabili per il template
            combined_models = user_settings.get('llm_model_name', '')
            models_parts = [model.strip() for model in combined_models.split(',')]
            
            user_settings['llm_model_name_primary'] = models_parts[0] if len(models_parts) > 0 else ''
            user_settings['llm_model_name_fallback'] = models_parts[1] if len(models_parts) > 1 else ''
    except sqlite3.Error as e:
        logger.error(f"Errore DB caricando le impostazioni per l'utente {user_id}: {e}")
        flash('Errore nel caricamento delle impostazioni.', 'error')
    finally:
        if conn:
            conn.close()
            
    return render_template('settings.html', settings=user_settings)