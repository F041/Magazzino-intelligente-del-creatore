import pytest
import os
import tempfile
import shutil
import logging # Aggiungi per logging

from app.main import create_app, init_db
from app.config import TestConfig # Importa TestConfig da app.config

_TEST_DATA_DIR = None
logger = logging.getLogger(__name__) # Logger per i test

@pytest.fixture(scope='module')
def app_instance_for_tests(): # Rinomina per chiarezza, questa fixture crea l'istanza
    """Crea e configura una nuova istanza dell'app per ogni modulo di test."""
    global _TEST_DATA_DIR

    _TEST_DATA_DIR = tempfile.mkdtemp()
    logger.info(f"Test data directory created: {_TEST_DATA_DIR}")

    test_config_instance = TestConfig()
    test_config_instance._TEST_BASE_DIR = _TEST_DATA_DIR # Imposta il path base

    # Crea il file client_secrets fittizio
    # Utilizza il percorso dalla TestConfig
    with open(test_config_instance.CLIENT_SECRETS_PATH, 'w') as f:
        f.write('{"installed":{"client_id":"test","project_id":"test","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","auth_provider_x509_cert_url":"https://www.googleapis.com/oauth2/v1/certs","client_secret":"test","redirect_uris":["http://localhost:5000/oauth2callback"]}}') # Contenuto fittizio


    app = create_app(test_config_instance) # Passa l'istanza della config

    with app.app_context():
        init_db(app.config) # init_db ora userà i path temporanei da TestConfig
        # Crea le directory se non esistono (anche se init_db dovrebbe gestirle)
        os.makedirs(app.config['UPLOAD_FOLDER_PATH'], exist_ok=True)
        os.makedirs(app.config['ARTICLES_FOLDER_PATH'], exist_ok=True)
        # Non serve creare CHROMA_PERSIST_PATH esplicitamente, ChromaDB lo farà

        # Se hai un riferimento al client ChromaDB sull'app, potresti usarlo.
        # Altrimenti, è difficile chiuderlo esplicitamente qui se è inizializzato dentro create_app
        # e non esposto.

    yield app # Fornisce l'app ai test

    # --- Teardown ---
    logger.info("Teardown: Attempting to clean up resources...")

    # Tentativo di resettare/chiudere il client ChromaDB se accessibile
    # Questo è speculativo e dipende da come è strutturata la tua app
    if hasattr(app, 'config') and 'CHROMA_CLIENT' in app.config and app.config['CHROMA_CLIENT'] is not None:
        try:
            logger.info("Attempting to reset ChromaDB client...")
            # Il client ChromaDB è memorizzato in app.config['CHROMA_CLIENT'] in create_app
            chroma_client_instance = app.config['CHROMA_CLIENT']
            chroma_client_instance.reset() # Questo cancella i dati, ma potrebbe chiudere i file.
            # Oppure prova a fermare i componenti se reset() non basta o è troppo distruttivo
            # if hasattr(chroma_client_instance, '_system'):
            #     chroma_client_instance._system.stop()
            logger.info("ChromaDB client reset/stop attempted.")
        except Exception as e:
            logger.warning(f"Error during ChromaDB client reset/stop in teardown: {e}")

    # Rimuovi la directory temporanea dei dati
    # Aggiungi un piccolo ritardo e tentativi per Windows
    # (brutto workaround ma a volte necessario per i file lock)
    import time
    for i in range(3): # Prova 3 volte
        try:
            logger.info(f"Attempting to remove test data directory: {_TEST_DATA_DIR} (Attempt {i+1})")
            if _TEST_DATA_DIR and os.path.exists(_TEST_DATA_DIR):
                shutil.rmtree(_TEST_DATA_DIR)
                logger.info(f"Test data directory removed: {_TEST_DATA_DIR}")
                break # Successo, esci dal loop
        except PermissionError as e:
            logger.warning(f"PermissionError removing test data directory (Attempt {i+1}): {e}")
            if i < 2: # Se non è l'ultimo tentativo
                time.sleep(1) # Aspetta 1 secondo e riprova
            else:
                logger.error(f"Failed to remove test data directory after multiple attempts: {_TEST_DATA_DIR}")
                # Non rilanciare l'eccezione per non far fallire i test solo per la pulizia,
                # ma il log indicherà il problema.
        except Exception as e:
            logger.error(f"Unexpected error removing test data directory: {e}", exc_info=True)
            break


@pytest.fixture(scope='function')
def client(app_instance_for_tests): # Usa la fixture che crea l'app
    """Un client di test per l'app."""
    return app_instance_for_tests.test_client()


def test_index_page_loads(client):
    response = client.get('/')
    assert response.status_code in [200, 302]
