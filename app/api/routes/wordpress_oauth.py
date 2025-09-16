import logging
import sqlite3
import time
from flask import Blueprint, current_app, redirect, request, url_for, flash
from flask_login import login_required, current_user
from authlib.integrations.flask_client import OAuth

# Creiamo un logger specifico per questo file, per tenere traccia di cosa succede
logger = logging.getLogger(__name__)

# Creiamo un "Blueprint", che è come un mini-capitolo della nostra app,
# dedicato solo all'autenticazione con WordPress.
wordpress_oauth_bp = Blueprint('wordpress_oauth', __name__)

# Inizializziamo l'oggetto OAuth che useremo per parlare con Authlib
oauth = OAuth()

def init_oauth(app):
    """
    Questa funzione viene chiamata una volta all'avvio dell'app (da main.py)
    per configurare il nostro client OAuth per WordPress.
    """
    oauth.init_app(app)
    
    # Qui "registriamo" WordPress come un fornitore OAuth, usando le chiavi
    # che abbiamo messo nel nostro file .env
    oauth.register(
        name='wordpress',
        client_id=app.config.get('WORDPRESS_CLIENT_ID'),
        client_secret=app.config.get('WORDPRESS_CLIENT_SECRET'),
        access_token_url='https://public-api.wordpress.com/oauth2/token',
        authorize_url='https://public-api.wordpress.com/oauth2/authorize',
        client_kwargs={'scope': 'auth'}, # 'auth' è il permesso base che ci serve
    )

@wordpress_oauth_bp.route('/oauth/wordpress/start')
@login_required
def start_wordpress_auth():
    """
    Questo è l'endpoint che il pulsante "Connetti con WordPress" chiamerà.
    Il suo unico compito è reindirizzare l'utente alla pagina di login di WordPress.
    """
    logger.info(f"Utente {current_user.id} ha avviato l'autenticazione OAuth con WordPress.")
    
    # Questo è l'indirizzo a cui WordPress dovrà rimandare l'utente dopo il login.
    # `_external=True` è FONDAMENTALE perché genera un URL completo (https://...)
    redirect_uri = url_for('wordpress_oauth.wordpress_auth_callback', _external=True)
    
    # Usiamo la libreria Authlib per generare l'URL di autorizzazione corretto
    return oauth.wordpress.authorize_redirect(redirect_uri)

@wordpress_oauth_bp.route('/oauth/wordpress/callback')
@login_required
def wordpress_auth_callback():
    """
    Questo è l'endpoint che WordPress chiama dopo che l'utente ha approvato.
    Qui riceviamo le chiavi (token) e le salviamo nel nostro database.
    """
    try:
        logger.info(f"Ricevuto callback da WordPress per l'utente {current_user.id}.")
        # Authlib fa la magia: prende il codice temporaneo dall'URL e lo scambia
        # con i token di accesso permanenti.
        token = oauth.wordpress.authorize_access_token()

        # Estraiamo le informazioni importanti che WordPress ci ha dato
        access_token = token.get('access_token')
        blog_id = token.get('blog_id') # L'ID del sito dell'utente
        
        # WordPress non fornisce un refresh token in questo flusso,
        # ma i loro access token hanno una lunga durata.
        
        db_path = current_app.config.get('DATABASE_FILE')
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Salviamo le nuove chiavi nel database per l'utente corrente,
            # cancellando le vecchie credenziali (username/password applicazione)
            # per mantenere la configurazione pulita.
            cursor.execute("""
                UPDATE user_settings
                SET wordpress_access_token = ?,
                    wordpress_blog_id = ?,
                    wordpress_username = NULL,
                    wordpress_api_key = NULL
                WHERE user_id = ?
            """, (access_token, blog_id, current_user.id))
            
            # Se non esisteva una riga per questo utente, ne creiamo una nuova
            if cursor.rowcount == 0:
                cursor.execute("""
                    INSERT INTO user_settings (user_id, wordpress_access_token, wordpress_blog_id)
                    VALUES (?, ?, ?)
                """, (current_user.id, access_token, blog_id))

            conn.commit()
            logger.info(f"Token WordPress salvato con successo per l'utente {current_user.id}.")
            flash('Sito WordPress connesso con successo!', 'success')

        except sqlite3.Error as e:
            logger.error(f"Errore DB durante il salvataggio del token WordPress: {e}")
            flash('Errore durante il salvataggio della connessione al database.', 'error')
            if conn: conn.rollback()
        finally:
            if conn: conn.close()

    except Exception as e:
        logger.error(f"Errore durante il callback di WordPress OAuth: {e}", exc_info=True)
        flash(f'Si è verificato un errore durante la connessione a WordPress: {e}', 'error')

    # In ogni caso, rimandiamo l'utente alla pagina delle impostazioni
    return redirect(url_for('settings.settings_page'))