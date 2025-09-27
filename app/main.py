# --- Import Standard ---
import os
import sys
import json
import logging
import sqlite3
from datetime import datetime
import uuid
import zipfile
import time
import sys 
import shutil
import atexit
from .models.user import User
from .utils import generate_api_key
from .utils import generate_api_key, format_datetime_filter
from .core.setup import init_db, setup_chroma_directory, load_credentials, save_credentials
from .core.system_info import get_system_stats

  

# --- Import Flask e Correlati ---
from flask import ( Flask, jsonify, redirect, request, session, url_for,
                   render_template, current_app, flash, send_from_directory, Response, stream_with_context )
from flask_cors import CORS
from flask_login import LoginManager, login_required, login_user, logout_user, current_user

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

# --- Import Google Auth/API ---
from google_auth_oauthlib.flow import Flow

# Non importare google_exceptions qui se non serve direttamente in main.py

# --- Import Altri Moduli ---
import chromadb
from dotenv import load_dotenv # Utile caricarlo anche qui all'inizio

# --- Caricamento Configurazione Centralizzata ---
load_dotenv() # Carica .env prima di importare config
try:
    # Importa le configurazioni da config.py (nella root del progetto)
    from .config import config_by_name, BaseConfig
    from .api.routes.wordpress_oauth import wordpress_oauth_bp, init_oauth
    config_name = os.getenv('FLASK_ENV', 'default')
    AppConfig = config_by_name.get(config_name, config_by_name['default'])
    print(f"Trovata configurazione per l'ambiente: {config_name}")
except ImportError as e:
     print(f"ERRORE CRITICO: Impossibile importare la configurazione da config.py: {e}")
     print("Assicurati che config.py esista nella directory principale del progetto.")
     sys.exit(1)
except KeyError:
    print(f"ERRORE CRITICO: Nome configurazione '{config_name}' non trovato in config.py.")
    print(f"Nomi disponibili: {list(config_by_name.keys())}")
    sys.exit(1)

# --- Configurazione Logging (Iniziale - sarà affinata in create_app) ---
# Impostiamo un livello base qui, verrà configurato meglio nell'app
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Inizializzazione Estensioni ---
db_sqlalchemy = SQLAlchemy()

def shutdown_scheduler(scheduler_instance):
    """Funzione per spegnere lo scheduler in modo pulito."""
    # Aggiungi un controllo se l'istanza è None per sicurezza
    if scheduler_instance and scheduler_instance.running:
        logger.info("Spegnimento APScheduler...")
        try:
            scheduler_instance.shutdown()
            logger.info("APScheduler spento.")
        except Exception as e:
            logger.error(f"Errore durante lo spegnimento dello scheduler: {e}")


def _handle_startup_restore(config):
    """
    Controlla la presenza di un backup in attesa e lo ripristina.
    Questa funzione viene chiamata all'avvio dell'applicazione.
    """
    try:
        data_dir = os.path.dirname(config.get('DATABASE_FILE'))
        pending_restore_path = os.path.join(data_dir, 'pending_restore.zip')

        if os.path.exists(pending_restore_path):
            logger.warning("!!! RILEVATO BACKUP COMPLETO IN ATTESA DI RIPRISTINO !!!")
            
            db_path_target = config.get('DATABASE_FILE')
            chroma_path_target = config.get('CHROMA_PERSIST_PATH')
            
            # Un piccolo ritardo per dare tempo al sistema di rilasciare i file
            time.sleep(1) 
            
            logger.info("Cancellazione dei dati correnti (SQLite e ChromaDB)...")
            if os.path.exists(db_path_target):
                os.remove(db_path_target)
            if os.path.exists(chroma_path_target):
                shutil.rmtree(chroma_path_target)
            
            logger.info(f"Estrazione del backup '{pending_restore_path}'...")
            with zipfile.ZipFile(pending_restore_path, 'r') as zf:
                zf.extractall(data_dir)
            
            os.remove(pending_restore_path)
            
            logger.warning("!!! RIPRISTINO ESEGUITO. L'APPLICAZIONE VERRA' TERMINATA PER COMPLETARE IL CICLO DI RIAVVIO. !!!")
            sys.exit(0)
                
    except Exception as e:
        logger.error(f"ERRORE CRITICO DURANTE IL PROCESSO DI RIPRISTINO: {e}", exc_info=True)
        sys.exit(1)

# --- Factory Function per l'App Flask ---
def create_app(config_object=AppConfig):
    """Crea e configura l'istanza dell'app Flask."""
    app = Flask(__name__)
    app.config.from_object(config_object) # Carica config Flask prima


    _handle_startup_restore(app.config)

    # Configura ProxyFix per fidarsi degli header inviati da UN proxy.
    # Cloudflare Tunnel agisce come un proxy.
    # x_for=1 significa che si fida dell'header X-Forwarded-For per l'IP.
    # x_proto=1 significa che si fida dell'header X-Forwarded-Proto per lo schema (http/https).
    # x_host=1 significa che si fida dell'header X-Forwarded-Host per l'host.
    # x_port=1 e x_prefix=1 sono per altri header che potrebbero essere rilevanti.
    # È importante configurare il numero corretto di proxy. Se Cloudflare Tunnel
    # è l'unico proxy davanti alla tua app, allora i valori a 1 sono corretti.
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1
    )


    # --- Configura Logging di Flask ---
    # Determina il livello di log in base a FLASK_DEBUG o DEBUG nella config
    is_debug_mode = app.config.get('FLASK_DEBUG', app.config.get('DEBUG', False))
    log_level = logging.DEBUG if is_debug_mode else logging.INFO

    # Configura il root logger o il logger dell'app
    # Rimuovi la vecchia basicConfig se presente qui per evitare conflitti
    # logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Invece, configura il logger dell'app o il logger root in modo più controllato
    # Se vuoi che TUTTI i logger (inclusi quelli delle librerie) siano a DEBUG:
    logging.getLogger().setLevel(log_level) # Imposta il root logger
    # Oppure, solo per il logger della tua app (se usi logger = logging.getLogger(__name__) nei moduli):
    # logging.getLogger('app').setLevel(log_level) # 'app' è il nome del tuo package principale

    # Aggiungi un handler se non ne hai già uno configurato da Gunicorn o altrove
    # (Gunicorn di solito gestisce l'output su stdout/stderr)
    # Se i log non appaiono, potresti dover aggiungere esplicitamente un handler:
    if not logging.getLogger().hasHandlers(): # Controlla se il root logger ha già handler
        handler = logging.StreamHandler(sys.stdout) # O sys.stderr
        handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(handler)

    logger.info(f"Logging configurato a livello: {logging.getLevelName(log_level)}")


    # Validazioni Configurazioni Critiche (importante averle dopo from_object)
    if not app.config.get('SECRET_KEY'): logger.critical("SECRET_KEY mancante!"); sys.exit(1)
    if not app.config.get('GOOGLE_API_KEY'): logger.warning("GOOGLE_API_KEY mancante.")
    if not app.config.get('CLIENT_SECRETS_PATH') or not os.path.exists(app.config['CLIENT_SECRETS_PATH']):
        logger.critical(f"File segreti Google mancante: {app.config.get('CLIENT_SECRETS_PATH')}"); sys.exit(1)
    if not app.config.get('DATABASE_FILE'): logger.critical("DATABASE_FILE mancante!"); sys.exit(1)
    if not app.config.get('CHROMA_PERSIST_PATH'): logger.critical("CHROMA_PERSIST_PATH mancante!"); sys.exit(1)
    if os.getenv('OAUTHLIB_INSECURE_TRANSPORT') != '1' and app.config.get('DEBUG'):
         logger.warning("OAUTHLIB_INSECURE_TRANSPORT non è '1'. OAuth locale su HTTP potrebbe fallire.")

    # Configurazione SQLAlchemy (per JobStore APScheduler)
    db_uri = f"sqlite:///{app.config.get('DATABASE_FILE')}"
    if not db_uri.startswith("sqlite:///"): # Controllo base
        logger.critical("DATABASE_FILE non configurato correttamente per SQLAlchemy.")
        sys.exit(1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False # Best practice
    db_sqlalchemy.init_app(app)

    # Configura APScheduler e lo lega all'istanza dell'app
    app.scheduler = BackgroundScheduler(
        jobstores={'default': SQLAlchemyJobStore(url=app.config['SQLALCHEMY_DATABASE_URI'])},
        app=app,
        timezone="Europe/Rome"
    )


    # Inizializzazione DB SQLite (incluse tabelle monitoring) e Directory Chroma
    try:
        # Passa l'oggetto config all'inizializzazione
        init_db(app.config)
        setup_chroma_directory(app.config)
    except Exception as e:
        logger.critical(f"Fallimento inizializzazione DB/Directory: {e}", exc_info=True)
        sys.exit(1)

    # Inizializzazione ChromaDB Client e Collezioni (Logica Esistente)
    # ... (Il tuo codice per inizializzare chroma_client e le collezioni in base a APP_MODE) ...
    try:
        chroma_path = app.config['CHROMA_PERSIST_PATH']
        app_mode = app.config.get('APP_MODE', 'single')
        logger.info(f"Inizializzazione ChromaDB client: path={chroma_path} | APP_MODE='{app_mode}'")
        chroma_client = chromadb.PersistentClient(path=chroma_path)
        app.config['CHROMA_CLIENT'] = chroma_client

        if app_mode == 'single':
             # ... (logica get_or_create per collezioni single mode) ...
             video_collection_name = app.config.get('VIDEO_COLLECTION_NAME', 'video_transcripts')
             doc_collection_name = app.config.get('DOCUMENT_COLLECTION_NAME', 'document_content')
             article_collection_name = app.config.get('ARTICLE_COLLECTION_NAME', 'article_content')
             try: app.config['CHROMA_VIDEO_COLLECTION'] = chroma_client.get_or_create_collection(name=video_collection_name); logger.info(f"Collezione VIDEO '{video_collection_name}' pronta.")
             except Exception as e: logger.error(f"Errore collezione VIDEO: {e}"); app.config['CHROMA_VIDEO_COLLECTION'] = None
             try: app.config['CHROMA_DOC_COLLECTION'] = chroma_client.get_or_create_collection(name=doc_collection_name); logger.info(f"Collezione DOC '{doc_collection_name}' pronta.")
             except Exception as e: logger.error(f"Errore collezione DOC: {e}"); app.config['CHROMA_DOC_COLLECTION'] = None
             try: app.config['CHROMA_ARTICLE_COLLECTION'] = chroma_client.get_or_create_collection(name=article_collection_name); logger.info(f"Collezione ARTICLE '{article_collection_name}' pronta.")
             except Exception as e: logger.error(f"Errore collezione ARTICLE: {e}"); app.config['CHROMA_ARTICLE_COLLECTION'] = None
        elif app_mode == 'saas':
             logger.info("Modalità SAAS: Collezioni Chroma gestite dalle API per utente.")
             app.config['CHROMA_VIDEO_COLLECTION'] = None
             app.config['CHROMA_DOC_COLLECTION'] = None
             app.config['CHROMA_ARTICLE_COLLECTION'] = None
        # ... (eventuali verifiche aggiuntive) ...
    except Exception as e:
        logger.exception("Errore CRITICO durante inizializzazione ChromaDB.")
        app.config['CHROMA_CLIENT'] = None
        app.config['CHROMA_VIDEO_COLLECTION'] = None
        app.config['CHROMA_DOC_COLLECTION'] = None
        app.config['CHROMA_ARTICLE_COLLECTION'] = None
    # Inizializza Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message = "Per favore, effettua il login per accedere a questa pagina."
    login_manager.login_message_category = "info"
    init_oauth(app)
    logger.info("Servizio OAuth per WordPress inizializzato.")

    # --- INIZIO BLOCCO SCHEDULER CON LOCK PER GUNICORN ---
    if not app.config.get('TESTING', False):
        
        # 1. OGNI worker definisce il job.
        #    In questo modo, chiunque riceva una richiesta dall'interfaccia web
        #    conosce l'esistenza del job e può modificarlo.
        with app.app_context():
            job_id = 'check_monitored_sources_job'
            # Usiamo 'replace_existing=True' per assicurarci che il job sia definito,
            # ma senza creare duplicati. 'paused=True' assicura che non parta subito.
            from .scheduler_jobs import check_monitored_sources_job
            
            # Recuperiamo la configurazione iniziale dal file .env come prima
            unit = app.config.get('SCHEDULER_INTERVAL_UNIT', 'days')
            value = app.config.get('SCHEDULER_INTERVAL_VALUE', 1)
            hour = app.config.get('SCHEDULER_RUN_HOUR', 4)
            trigger_args, trigger_type = {}, 'cron'
            if unit == 'days':
                trigger_args = {'hour': hour, 'minute': 0, 'day': f'*/{value}'}
            elif unit == 'hours':
                trigger_args = {'minute': 0, 'hour': f'*/{value}'}
            elif unit == 'minutes':
                trigger_args = {'minute': f'*/{value}'}

            app.scheduler.add_job(
                func=check_monitored_sources_job,
                trigger=trigger_type,
                id=job_id,
                name='Controllo Periodico Sorgenti Monitorate',
                replace_existing=True,
                misfire_grace_time=300,
                **trigger_args
            )
            logger.info(f"Worker PID {os.getpid()}: Job '{job_id}' definito/aggiornato nella configurazione dello scheduler.")


        # 2. SOLO UN worker avvia effettivamente lo scheduler.
        #    Usiamo la stessa logica del lock file di prima.
        lock_file_path = os.path.join(os.path.dirname(app.config['DATABASE_FILE']), 'scheduler.lock')
        try:
            with open(lock_file_path, 'x') as lock_file:
                lock_file.write(str(os.getpid()))
                logger.info(f"Worker PID {os.getpid()} ha acquisito il lock. AVVIO DELLO SCHEDULER.")
                
                if not app.scheduler.running:
                    app.scheduler.start()
                    logger.info("APScheduler avviato con successo.")
                    atexit.register(lambda: os.remove(lock_file_path))
                    atexit.register(lambda: shutdown_scheduler(app.scheduler))

        except FileExistsError:
            logger.info(f"Worker PID {os.getpid()}: Lock file già presente. Lo scheduler è già in esecuzione in un altro processo.")
            
    else:
        # Codice per la modalità TESTING (invariato)
        logger.info("Modalità TESTING: APScheduler NON avviato.")


    @login_manager.user_loader
    def load_user(user_id):
        # Usa current_app.config qui!
        db_path = current_app.config.get('DATABASE_FILE')
        if not db_path: logger.error("User Loader: DATABASE_FILE non configurato!"); return None
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            user_data = cursor.fetchone()
            if user_data:
                user = User(id=user_data['id'], email=user_data['email'], password_hash=user_data['password_hash'], name=user_data['name'])
                return user
            return None
        except sqlite3.Error as e:
            logger.error(f"User Loader: Errore DB caricando utente {user_id}: {e}")
            return None
        finally:
            if conn: conn.close()



    # Abilita CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # Registra Blueprints (Usa import relativi corretti)
    try:
        app.register_blueprint(wordpress_oauth_bp)
        from .api.routes.videos import videos_bp
        app.register_blueprint(videos_bp, url_prefix='/api/videos')
        from .api.routes.search import search_bp
        app.register_blueprint(search_bp, url_prefix='/api/search')
        from .api.routes.documents import documents_bp
        app.register_blueprint(documents_bp, url_prefix='/api/documents')
        from .api.routes.rss import rss_bp
        app.register_blueprint(rss_bp, url_prefix='/api/rss')
        from .api.routes.website import connectors_bp
        app.register_blueprint(connectors_bp, url_prefix='/api/website')
        from .api.routes.keys import keys_bp
        app.register_blueprint(keys_bp, url_prefix='/keys')
        logger.info("Blueprint Keys (gestione e API) registrato con prefisso /keys.")
        from .api.routes.monitoring import monitoring_bp
        app.register_blueprint(monitoring_bp, url_prefix='/api/monitoring')
        logger.info("Blueprint Monitoring registrato con prefisso /api/monitoring.")
        from .api.routes.settings import settings_bp
        app.register_blueprint(settings_bp) # Nessun prefisso, la rotta è /settings
        #from .api.routes.wordpress_oauth import wordpress_oauth_bp, init_oauth
        logger.info("Blueprint Settings registrato.")

        from .api.routes.protection import protection_bp
        app.register_blueprint(protection_bp, url_prefix='/api/protection')
        logger.info("Blueprint Protection registrato con prefisso /api/protection.")

        from .api.routes.statistics import stats_bp
        app.register_blueprint(stats_bp)
        logger.info("Blueprint Statistics registrato.")
        from .api.routes.ideas import ideas_bp
        app.register_blueprint(ideas_bp, url_prefix='/api/ideas')
        logger.info("Blueprint Ideas (generatore idee) registrato con prefisso /api/ideas.")
    except ImportError as e:
         logger.critical(f"Errore importazione/registrazione blueprint: {e}", exc_info=True)
         sys.exit(1)

    CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    logger.info("Configurazione CORS applicata a tutta l'applicazione.")

    app.jinja_env.filters['format_date'] = format_datetime_filter

    @app.route('/')
    def index():
        """Mostra sempre la pagina di benvenuto (index.html)."""
        return render_template('index.html')

    @app.route('/authorize')
    def authorize():
        secrets_path = current_app.config.get('CLIENT_SECRETS_PATH')
        scopes = current_app.config.get('GOOGLE_SCOPES')
        if not secrets_path or not os.path.exists(secrets_path) or not scopes:
            logger.error("Configurazione OAuth mancante per /authorize.")
            return "Errore: Configurazione OAuth server incompleta.", 500
        try:
            redirect_uri = url_for('oauth2callback', _external=True)
            logger.info(f"!!! DEBUG: Generated Redirect URI for Google Flow: '{redirect_uri}'")
            flow = Flow.from_client_secrets_file(secrets_path, scopes=scopes, redirect_uri=redirect_uri)
            authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
            session['oauth_state'] = state
            logger.info(f"Redirecting per autorizzazione OAuth. State: {state}")
            return redirect(authorization_url)
        except Exception as e:
            logger.exception("Errore durante /authorize flow.")
            return jsonify({'success': False, 'error_code': 'OAUTH_AUTHORIZATION_FAILED', 'message': f"Authorization error: {str(e)}"}), 500

    @app.route('/oauth2callback')
    def oauth2callback():
        state = session.pop('oauth_state', None)
        # Controlli stato e errore da URL args
        if not state or state != request.args.get('state'):
             logger.error("OAuth State Mismatch!")
             return jsonify({'success': False, 'error_code': 'OAUTH_STATE_MISMATCH', 'message': 'Invalid state parameter.'}), 400
        if request.args.get('error'):
            error_msg = request.args.get('error')
            logger.error(f"OAuth Error da Google: {error_msg}")
            return jsonify({'success': False, 'error_code': 'OAUTH_ACCESS_DENIED', 'message': f'OAuth failed: {error_msg}'}), 403

        # Configurazione e fetch token
        secrets_path = current_app.config.get('CLIENT_SECRETS_PATH')
        scopes = current_app.config.get('GOOGLE_SCOPES')
        if not secrets_path or not os.path.exists(secrets_path) or not scopes:
             logger.error("Configurazione OAuth mancante per /oauth2callback.")
             return jsonify({'success': False, 'error_code': 'OAUTH_SERVER_CONFIG_ERROR', 'message': 'OAuth server configuration error.'}), 500
        try:
            redirect_uri = url_for('oauth2callback', _external=True)
            flow = Flow.from_client_secrets_file(secrets_path, scopes=scopes, state=state, redirect_uri=redirect_uri)
            auth_resp = request.url
            if os.getenv("OAUTHLIB_INSECURE_TRANSPORT") == "1" and auth_resp.startswith('http://'):
                 auth_resp = auth_resp.replace('http://', 'https://', 1)
                 logger.warning(f"Rewriting callback URL to HTTPS for token fetch: {auth_resp}")
            flow.fetch_token(authorization_response=auth_resp)
            credentials = flow.credentials
            logger.info(f"Token OAuth ottenuto. Refresh token presente: {'yes' if credentials.refresh_token else 'no'}")
            save_credentials(credentials) # Richiede contesto app
            return redirect(url_for('data_entry'))
        except Exception as e:
            logger.exception("Errore durante fetch_token in /oauth2callback.")
            return jsonify({'success': False, 'error_code': 'OAUTH_TOKEN_FETCH_FAILED', 'message': f"Token fetch error: {str(e)}"}), 500

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('data_entry'))

        if request.method == 'POST':
            email = request.form.get('email')
            password = request.form.get('password')

            if not email or not password:
                flash('Email e password sono richiesti.', 'error')
                return redirect(url_for('login'))

            # Trova utente nel DB
            user = None
            db_path = current_app.config.get('DATABASE_FILE')
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
                user_data = cursor.fetchone()
                if user_data:
                    user = User(id=user_data['id'], email=user_data['email'], password_hash=user_data['password_hash'], name=user_data['name'])
            except sqlite3.Error as e:
                logger.error(f"Errore DB durante login per email {email}: {e}")
                flash('Errore durante il login. Riprova più tardi.', 'error')
                return redirect(url_for('login'))
            finally:
                if conn: conn.close()

            # Verifica password e logga utente
            if user and user.check_password(password):
                login_user(user)
                logger.info(f"Utente {user.email} loggato con successo.")
                next_page = request.args.get('next')
                if not next_page: # Se 'next' non esiste, vai ad ingresso dati
                 next_page = url_for('data_entry')
                 return redirect(next_page)
            else:
                flash('Email o password non validi.', 'error')
                return redirect(url_for('login'))

        return render_template('login.html')

    @app.route('/logout')
    @login_required # Assicura che solo utenti loggati possano fare logout
    def logout():
        """Effettua il logout dell'utente."""
        user_email = current_user.email # Prendi l'email prima del logout per il log
        logout_user() # Funzione chiave di Flask-Login che pulisce la sessione
        logger.info(f"Utente {user_email} sloggato.")
        flash('Sei stato disconnesso.', 'info') # Messaggio opzionale
        return redirect(url_for('login')) # Reindirizza alla pagina di login

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for('data_entry'))

        if request.method == 'POST':
            # --- INIZIO LOGICA DI CONTROLLO "LISTA INVITATI" (con DEBUG) ---
            allowed_emails_str = os.getenv('ALLOWED_EMAILS')
            logger.debug(f"CONTROLLO REGISTRAZIONE: Valore di ALLOWED_EMAILS da .env: '{allowed_emails_str}'")

            if allowed_emails_str:
                allowed_emails = [email.strip() for email in allowed_emails_str.split(',')]
                user_email_to_register = request.form.get('email')
                
                logger.debug(f"CONTROLLO REGISTRAZIONE: Email da registrare: '{user_email_to_register}'")
                logger.debug(f"CONTROLLO REGISTRAZIONE: Lista email permesse: {allowed_emails}")
                
                if user_email_to_register not in allowed_emails:
                    logger.warning(f"BLOCCO REGISTRAZIONE: L'email '{user_email_to_register}' non e' in lista. Blocco.")
                    
                    custom_message = os.getenv('CUSTOM_REGISTRATION_DENIED_MESSAGE')
                    contact_link = os.getenv('CUSTOM_CONTACT_LINK')
                    
                    if custom_message and contact_link and contact_link.startswith(('http://', 'https://')):
                        from markupsafe import Markup
                        final_message = Markup(f'{custom_message} <a href="{contact_link}" target="_blank" rel="noopener noreferrer">Clicca qui</a>.')
                    elif custom_message:
                        final_message = custom_message
                    else:
                        final_message = "Non sei autorizzato a registrare un account."

                    flash(final_message, "error")
                    return redirect(url_for('register'))
                else:
                    logger.debug(f"CONTROLLO REGISTRAZIONE: L'email '{user_email_to_register}' e' in lista. Procedo.")
            else:
                logger.debug("CONTROLLO REGISTRAZIONE: ALLOWED_EMAILS e' vuota, registrazione aperta a tutti.")
            # --- FINE LOGICA DI CONTROLLO ---

            # Il resto della funzione originale per la registrazione effettiva
            email = request.form.get('email')
            name = request.form.get('name')
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')

            if not email or not password or not confirm_password:
                flash('Email, password e conferma password sono richiesti.', 'error')
                return redirect(url_for('register'))
            if password != confirm_password:
                flash('Le password non coincidono.', 'error')
                return redirect(url_for('register'))
            if len(password) < 8:
                 flash('La password deve essere lunga almeno 8 caratteri.', 'error')
                 return redirect(url_for('register'))

            db_path = current_app.config.get('DATABASE_FILE')
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
                existing_user = cursor.fetchone()
                if existing_user:
                    flash('Email già registrata. Effettua il login.', 'error')
                    return redirect(url_for('register'))

                new_user_id = User.generate_id()
                new_user = User(id=new_user_id, email=email, name=name if name else None)
                new_user.set_password(password)

                cursor.execute("INSERT INTO users (id, email, password_hash, name) VALUES (?, ?, ?, ?)",
                               (new_user.id, new_user.email, new_user.password_hash, new_user.name))
                conn.commit()
                logger.info(f"Nuovo utente registrato: {new_user.email} (ID: {new_user.id})")
                flash('Registrazione completata! Ora puoi effettuare il login.', 'info')
                return redirect(url_for('login'))

            except sqlite3.Error as e:
                logger.error(f"Errore DB durante registrazione per email {email}: {e}")
                if conn: conn.rollback()
                flash('Errore durante la registrazione. Riprova più tardi.', 'error')
                return redirect(url_for('register'))
            finally:
                if conn: conn.close()

        return render_template('register.html')


    @app.route('/data-entry')
    @login_required
    def data_entry():
        return render_template('data_entry.html')

    @app.route('/my-videos')
    @login_required
    def my_videos():
        db_path = current_app.config.get('DATABASE_FILE')
        app_mode = current_app.config.get('APP_MODE', 'single')

        current_user_id = current_user.id if current_user.is_authenticated else None

        if app_mode == 'saas' and not current_user_id:
             # Questo non dovrebbe succedere a causa di @login_required, ma per sicurezza
             logger.error("/my-videos: Modalità SAAS ma utente non autenticato (questo è strano).")
             flash("Errore: Utente non identificato.", "error")
             return redirect(url_for('login'))
        elif app_mode == 'saas':
             logger.info(f"/my-videos: Modalità SAAS, filtro per user '{current_user_id}'")
        else:
             logger.info(f"/my-videos: Modalità SINGLE, mostro tutti i video.")
             current_user_id = None # Assicura sia None in single mode per la query

        videos_from_db = []
        try:
            if not db_path: raise ValueError("Percorso DB non configurato")
            conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
            sql_query = 'SELECT * FROM videos'
            params = []
            if app_mode == 'saas':
                sql_query += ' WHERE user_id = ?'
                params.append(current_user_id) # Usa l'ID reale
            sql_query += ' ORDER BY published_at DESC'
            cursor.execute(sql_query, tuple(params))
            videos_from_db = cursor.fetchall()
            conn.close()
            logger.info(f"/my-videos: Recuperati {len(videos_from_db)} video dal DB.")
        except (sqlite3.Error, ValueError) as e:
            logger.error(f"Errore lettura DB per /my-videos: {e}")
        return render_template('my_videos.html',
                           videos=videos_from_db,
                           config=current_app.config)

    @app.route('/my-documents')
    @login_required
    def my_documents():
        """Mostra la pagina con l'elenco dei documenti caricati."""
        app_mode = current_app.config.get('APP_MODE', 'single') # Leggi APP_MODE
        current_user_id = current_user.id if current_user.is_authenticated else None

        if app_mode == 'saas' and not current_user_id:
             return redirect(url_for('login'))
        elif app_mode == 'saas': logger.info(f"/my-documents: Modalità SAAS, filtro per user '{current_user_id}'")
        else: logger.info(f"/my-documents: Modalità SINGLE, mostro tutti i documenti."); current_user_id = None

        documents_from_db = []
        db_path = current_app.config.get('DATABASE_FILE')
        if not db_path:
            logger.error("Percorso DATABASE_FILE non configurato per /my-documents.")
        else:
            try:
                conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
                sql_query = 'SELECT * FROM documents'
                params = []
                if app_mode == 'saas':
                    sql_query += ' WHERE user_id = ?'
                    params.append(current_user_id)
                sql_query += ' ORDER BY uploaded_at DESC'
                cursor.execute(sql_query, tuple(params))
                documents_from_db = cursor.fetchall()
                conn.close()
                logger.info(f"/my-documents: Recuperati {len(documents_from_db)} documenti dal DB.")
            except sqlite3.Error as e:
                logger.error(f"Errore lettura DB per /my-documents: {e}")

        return render_template('my_documents.html', documents=documents_from_db)

    @app.route('/my-articles')
    @login_required
    def my_articles():
        app_mode = current_app.config.get('APP_MODE', 'single')
        current_user_id = current_user.id if current_user.is_authenticated else None

        if app_mode == 'saas' and not current_user_id:
            return redirect(url_for('login'))

        articles_from_db = []
        db_path = current_app.config.get('DATABASE_FILE')
        if not db_path:
            logger.error("Percorso DATABASE_FILE non configurato per /my-articles.")
        else:
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                sql_query = 'SELECT * FROM articles'
                params = []
                if app_mode == 'saas':
                    sql_query += ' WHERE user_id = ?'
                    params.append(current_user_id)
                sql_query += ' ORDER BY added_at DESC'
                
                cursor.execute(sql_query, tuple(params))
                
                # --- LOGICA OTTIMIZZATA ---
                # 1. Recupera tutti i dati dal DB in una sola volta
                all_rows = cursor.fetchall()
                conn.close() # Chiudiamo subito la connessione al DB

                # 2. Ora cicliamo sui dati in memoria per generare l'anteprima
                for row in all_rows:
                    article_dict = dict(row)
                    content = article_dict.get('content', '')
                    # Crea un'anteprima prendendo i primi 200 caratteri
                    article_dict['content_preview'] = (content[:200] if content else '')
                    articles_from_db.append(article_dict)

                logger.info(f"/my-articles: Recuperati e processati {len(articles_from_db)} articoli.")
            except sqlite3.Error as e:
                logger.error(f"Errore lettura DB per /my-articles: {e}")

        return render_template('my_articles.html', articles=articles_from_db, config=current_app.config)

    @app.route('/my-pages')
    @login_required
    def my_pages():
        app_mode = current_app.config.get('APP_MODE', 'single')
        current_user_id = current_user.id if current_user.is_authenticated else None

        if app_mode == 'saas' and not current_user_id:
            return redirect(url_for('login'))

        pages_from_db = []
        db_path = current_app.config.get('DATABASE_FILE')
        if not db_path:
            logger.error("Percorso DATABASE_FILE non configurato per /my-pages.")
        else:
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                sql_query = 'SELECT * FROM pages'
                params = []
                if app_mode == 'saas':
                    sql_query += ' WHERE user_id = ?'
                    params.append(current_user_id)
                sql_query += ' ORDER BY added_at DESC'
                
                cursor.execute(sql_query, tuple(params))

                # --- LOGICA OTTIMIZZATA ---
                all_rows = cursor.fetchall()
                conn.close() # Chiudi subito la connessione al DB

                for row in all_rows:
                    page_dict = dict(row)
                    content = page_dict.get('content', '')
                    # Crea un'anteprima prendendo i primi 200 caratteri
                    page_dict['content_preview'] = (content[:200] if content else '')
                    pages_from_db.append(page_dict)
                
                logger.info(f"/my-pages: Recuperate e processate {len(pages_from_db)} pagine.")
            except sqlite3.Error as e:
                logger.error(f"Errore lettura DB per /my-pages: {e}")

        return render_template('my_pages.html', pages=pages_from_db, config=current_app.config)
    
    @app.route('/api/docs')
    def api_docs():
         with app.app_context(): # Necessario per load_credentials
             if not load_credentials(): return redirect('/')
         # Considera di usare un vero file template
         return render_template('api_docs.html') # Assumendo che tu crei questo template

    @app.route('/chat')
    @login_required # L'utente deve essere loggato per usare la chat
    def chat_page():
        """Renderizza la pagina dell'interfaccia chat."""
        logger.info(f"Accesso alla pagina /chat da utente {current_user.id}")
        # Non servono dati specifici da passare al template per ora
        return render_template('chat.html')

    @app.route('/widget')
    def widget_content():
        logger.info("Richiesta per /widget (contenuto iframe).")
        return render_template('widget.html')

    @app.route('/embed.js', endpoint='serve_embed_js')
    def serve_embed_js():
        static_js_dir = os.path.join(current_app.static_folder, 'js')
        logger.debug(f"Tentativo di servire embed.js da: {static_js_dir}")
        try:
            if not os.path.isdir(static_js_dir):
                logger.error(f"Directory statica JS non trovata: {static_js_dir}")
                return "Internal Server Error: Static JS directory not found.", 500
            if not os.path.isfile(os.path.join(static_js_dir, 'embed.js')):
                logger.error(f"File embed.js non trovato in: {static_js_dir}")
                return "Not Found: embed.js not found.", 404
            return send_from_directory(static_js_dir, 'embed.js', mimetype='application/javascript')
        except Exception as e:
            logger.error(f"Errore imprevisto in serve_embed_js: {e}", exc_info=True)
            return "Internal Server Error", 500


    @app.route('/automations')
    @login_required
    def automations_page():
        source_type = request.args.get('type')
        source_url = request.args.get('url')
        logger.info(f"Accesso pagina /automations. Parametri: type={source_type}, url={source_url}")
        return render_template('automations.html',
                               initial_type=source_type,
                               initial_url=source_url)

    @app.route('/generate-link')
    @login_required
    def generate_link_page():
        """Renderizza la pagina per generare token di accesso JWT per la chat esterna."""
        # Se in futuro implementeremo i ruoli, basterà cambiare @login_required in @admin_required
        return render_template('generate_token.html')
    
    @app.route('/widget-standalone')
    def widget_standalone_page():   
        """
        Renderizza la pagina della chat standalone, che si aspetta un token JWT
        come parametro URL per l'autenticazione via JavaScript.
        """
        return render_template('widget_standalone.html')
    
    @app.route('/privacy-policy')
    def privacy_policy_page():
        """Renderizza la pagina della Privacy Policy."""
        return render_template('legal/privacy_policy.html')

    @app.route('/terms-of-service')
    def terms_of_service_page():
        """Renderizza la pagina dei Termini di Servizio."""
        return render_template('legal/terms_of_service.html')
    
    @app.route('/personalization', methods=['GET', 'POST'])
    @login_required
    def personalization_page():
        """
        Gestisce la visualizzazione (GET) e il salvataggio (POST) 
        delle impostazioni di personalizzazione.
        """
        user_id = current_user.id
        db_path = current_app.config.get('DATABASE_FILE')
        conn = None

        if request.method == 'POST':
            try:
                # Recupera i dati dal form
                settings_to_save = {
                    'brand_color': request.form.get('brand_color'),
                    'brand_logo_url': request.form.get('brand_logo_url'),
                    'welcome_message': request.form.get('welcome_message'),
                    'prompt_starter_1': request.form.get('prompt_starter_1'),
                    'prompt_starter_2': request.form.get('prompt_starter_2'),
                    'prompt_starter_3': request.form.get('prompt_starter_3'),
                    'brand_font': request.form.get('brand_font')
                }
                
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()

                # Usa INSERT OR IGNORE per assicurarsi che esista una riga per l'utente,
                # senza sovrascrivere dati esistenti se la riga c'è già.
                cursor.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))

                # Esegui l'UPDATE per salvare le nuove impostazioni
                cursor.execute("""
                    UPDATE user_settings SET
                        brand_color = ?,
                        brand_logo_url = ?,
                        welcome_message = ?,
                        prompt_starter_1 = ?,
                        prompt_starter_2 = ?,
                        prompt_starter_3 = ?,
                        brand_font = ?
                    WHERE user_id = ?
                """, (
                    settings_to_save['brand_color'],
                    settings_to_save['brand_logo_url'],
                    settings_to_save['welcome_message'],
                    settings_to_save['prompt_starter_1'],
                    settings_to_save['prompt_starter_2'],
                    settings_to_save['prompt_starter_3'],
                    settings_to_save['brand_font'],
                    user_id
                ))
                
                conn.commit()
                flash('Impostazioni di personalizzazione salvate con successo!', 'success')
                logger.info(f"Impostazioni di personalizzazione salvate per l'utente {user_id}.")

            except sqlite3.Error as e:
                if conn: conn.rollback()
                flash('Errore durante il salvataggio delle impostazioni nel database.', 'error')
                logger.error(f"Errore DB salvando personalizzazione per utente {user_id}: {e}")
            finally:
                if conn: conn.close()

            return redirect(url_for('personalization_page'))

        # Logica per il metodo GET (mostrare la pagina con i dati salvati)
        user_settings = {}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
            settings_row = cursor.fetchone()
            if settings_row:
                user_settings = dict(settings_row)
        except sqlite3.Error as e:
            logger.error(f"Errore DB caricando personalizzazione per utente {user_id}: {e}")
        finally:
            if conn: conn.close()
            
        return render_template('personalization.html', settings=user_settings)
    
    @app.route('/personalization/reset')
    @login_required
    def reset_personalization():
        """Resetta le impostazioni di personalizzazione dell'utente a NULL."""
        user_id = current_user.id
        db_path = current_app.config.get('DATABASE_FILE')
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE user_settings SET
                    brand_color = NULL,
                    brand_logo_url = NULL,
                    welcome_message = NULL,
                    prompt_starter_1 = NULL,
                    prompt_starter_2 = NULL,
                    prompt_starter_3 = NULL
                WHERE user_id = ?
            """, (user_id,))
            conn.commit()
            flash('Le impostazioni di personalizzazione sono state ripristinate.', 'success')
            logger.info(f"Impostazioni di personalizzazione ripristinate per l'utente {user_id}.")
        except sqlite3.Error as e:
            if conn: conn.rollback()
            flash('Errore durante il ripristino delle impostazioni.', 'error')
            logger.error(f"Errore DB durante il ripristino della personalizzazione per {user_id}: {e}")
        finally:
            if conn: conn.close()
        
        return redirect(url_for('personalization_page'))
    
    @app.route('/system-status')
    @login_required
    def system_status_page():
        """
        Mostra la pagina con le statistiche tecniche e lo stato di salute del sistema.
        """
        # Chiama la funzione esterna per ottenere tutti i dati
        system_stats_data = get_system_stats()
        
        # Passa i dati al template per la visualizzazione
        return render_template('system_status.html', stats_data=system_stats_data)

    @app.context_processor
    def inject_global_data():
        """
        Rende disponibili dati globali a tutti i template:
        - Lo stato dei contenuti dell'utente (content_counts).
        - Le impostazioni di personalizzazione dell'utente (personalization_settings).
        """
        # Importiamo current_user QUI DENTRO per essere sicuri che sia disponibile.
        from flask_login import current_user

        # Inizializza i valori di default che verranno usati se l'utente non è loggato
        # o se non ha impostazioni personalizzate.
        content_counts = {'has_videos': False, 'has_documents': False, 'has_articles': False, 'has_pages': False}
        personalization_settings = {
            'brand_color': '#007bff', # Colore di default
            'brand_logo_url': None,
            'welcome_message': "<p>Ciao! Sono l'assistente del Magazzino del Creatore.</p><p>Fai una domanda sui contenuti che hai indicizzato (video, documenti, articoli).</p>",
            'prompt_starters': [],
            'brand_font': 'Segoe UI, sans-serif' 
        }

        # Controlliamo in modo sicuro se l'utente è autenticato.
        # Questo è il modo più robusto per farlo in un context_processor.
        if current_user and hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
            db_path = current_app.config.get('DATABASE_FILE')
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                user_filter = " WHERE user_id = ?"
                params = (current_user.id,)

                # --- Logica per content_counts (invariata) ---
                cursor.execute(f"SELECT 1 FROM videos {user_filter} AND processing_status = 'completed' LIMIT 1", params)
                if cursor.fetchone(): content_counts['has_videos'] = True
                cursor.execute(f"SELECT 1 FROM documents {user_filter} AND processing_status = 'completed' LIMIT 1", params)
                if cursor.fetchone(): content_counts['has_documents'] = True
                cursor.execute(f"SELECT 1 FROM articles {user_filter} AND processing_status = 'completed' LIMIT 1", params)
                if cursor.fetchone(): content_counts['has_articles'] = True
                cursor.execute(f"SELECT 1 FROM pages {user_filter} AND processing_status = 'completed' LIMIT 1", params)
                if cursor.fetchone(): content_counts['has_pages'] = True

                # --- Logica per caricare le impostazioni di personalizzazione ---
                cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (current_user.id,))
                settings_row = cursor.fetchone()
                if settings_row:
                    # Sovrascrivi i default solo se i valori nel DB non sono vuoti
                    if settings_row['brand_color']: 
                        personalization_settings['brand_color'] = settings_row['brand_color']
                    if settings_row['brand_logo_url']: 
                        personalization_settings['brand_logo_url'] = settings_row['brand_logo_url']
                    if settings_row['welcome_message']: 
                        personalization_settings['welcome_message'] = settings_row['welcome_message']
                    if settings_row['brand_font']: 
                        personalization_settings['brand_font'] = settings_row['brand_font']
                    
                    starters = [
                        settings_row['prompt_starter_1'],
                        settings_row['prompt_starter_2'],
                        settings_row['prompt_starter_3']
                    ]
                    # Filtra via i valori None o stringhe vuote
                    personalization_settings['prompt_starters'] = [s for s in starters if s]

            except sqlite3.Error as e:
                logger.error(f"Errore DB in context_processor: {e}")
            finally:
                if conn: conn.close()
        
        # Rendi entrambe le variabili disponibili in tutti i template
        return dict(
            content_counts=content_counts,
            personalization_settings=personalization_settings
        )
    return app


# --- Blocco Esecuzione Principale (__main__) ---
if __name__ == '__main__':
    # Chiama la factory e salva l'istanza restituita
    app_instance = create_app(AppConfig)

    # Verifica che l'istanza sia valida prima di usarla
    if app_instance:
        host = os.getenv('FLASK_RUN_HOST', '127.0.0.1')
        print(f"DEBUG: Valore di 'host' letto da os.getenv: '{host}'")
        port = int(os.getenv('FLASK_RUN_PORT', 5000))
        use_debug = app_instance.config.get('DEBUG', False) # DEBUG viene da AppConfig

        # Per il server di sviluppo Flask (quando __name__ == '__main__'):
        # il reloader è generalmente attivo se debug è attivo.
        # In produzione con Gunicorn, Gunicorn stesso gestisce i worker e il reload (se configurato).
        use_reloader = use_debug

        logger.info(f"Avvio Flask DEV server http://{host}:{port} | Debug={use_debug}, Reloader={use_reloader}")
        # Esegui l'app usando l'istanza locale
        # Aggiungiamo 'exclude_patterns' per impedire al reloader di scattare per i file .zip
        extra_files = []
        exclude_patterns = ['**/pending_restore.zip']

        logger.info(f"Avvio Flask DEV server http://{host}:{port} | Debug={use_debug}, Reloader={use_reloader}")
        # Esegui l'app usando l'istanza locale, con l'opzione per ignorare i file
        app_instance.run(host=host, port=port, debug=use_debug, use_reloader=use_reloader, exclude_patterns=exclude_patterns)
    else:
        logger.critical("Impossibile avviare: la factory create_app non ha restituito un'istanza Flask valida.")
