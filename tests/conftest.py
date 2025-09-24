import pytest
import os
import tempfile
import shutil
import logging

# Importa dall'applicazione. Assicurati che il PYTHONPATH sia corretto
# o che pytest sia eseguito dalla radice del progetto.
from app.main import create_app, init_db
from app.config import TestConfig

_TEST_DATA_DIR_CONFTEST = None # Usa un nome diverso per evitare conflitti se test_app.py viene eseguito
logger = logging.getLogger(__name__)

@pytest.fixture(scope='session')
def app():
    global _TEST_DATA_DIR_CONFTEST
    _TEST_DATA_DIR_CONFTEST = tempfile.mkdtemp(prefix="pytest_magazzino_session_")
    logger.info(f"CONFTEST: Test data directory created: {_TEST_DATA_DIR_CONFTEST}")

    test_config_instance = TestConfig()
    test_config_instance._TEST_BASE_DIR = _TEST_DATA_DIR_CONFTEST

    client_secrets_path_for_test = test_config_instance.CLIENT_SECRETS_PATH
    os.makedirs(os.path.dirname(client_secrets_path_for_test), exist_ok=True)
    with open(client_secrets_path_for_test, 'w') as f:
        f.write('{"installed":{"client_id":"test", "client_secret":"test"}}') # Contenuto più realistico
    logger.info(f"CONFTEST: Dummy client_secrets.json created at: {client_secrets_path_for_test}")

    flask_app = create_app(test_config_instance)
    
    with flask_app.app_context():
        init_db(flask_app.config)
        logger.info(f"CONFTEST: Test database initialized at: {flask_app.config['DATABASE_FILE']}")
        os.makedirs(flask_app.config['UPLOAD_FOLDER_PATH'], exist_ok=True)
        os.makedirs(flask_app.config['ARTICLES_FOLDER_PATH'], exist_ok=True)

    yield flask_app

    logger.info(f"CONFTEST: Teardown for session-scoped app fixture.")
    
    # 1. Spegni ChromaDB PRIMA di tentare di cancellare i file
    if hasattr(flask_app, 'config') and 'CHROMA_CLIENT' in flask_app.config:
        chroma_client_instance = flask_app.config.get('CHROMA_CLIENT')
        # Controlliamo che il client e il sistema interno esistano prima di chiamare stop()
        if chroma_client_instance and hasattr(chroma_client_instance, '_system') and hasattr(chroma_client_instance._system, 'stop'):
            try:
                logger.info("CONFTEST: Attempting to stop ChromaDB internal system...")
                chroma_client_instance._system.stop()
                logger.info("CONFTEST: ChromaDB internal system stop called.")
                # Diamo un istante al sistema per rilasciare i file
                import time
                time.sleep(0.5)
            except Exception as e_chroma_stop:
                logger.warning(f"CONFTEST: Error trying to stop ChromaDB system: {e_chroma_stop}")

    # 2. Ora procedi con la cancellazione della cartella
    if _TEST_DATA_DIR_CONFTEST and os.path.exists(_TEST_DATA_DIR_CONFTEST):
        import time
        # Aggiungiamo dei tentativi per rendere la pulizia più robusta
        for i in range(3):
            try:
                shutil.rmtree(_TEST_DATA_DIR_CONFTEST)
                logger.info(f"CONFTEST: Test data directory removed: {_TEST_DATA_DIR_CONFTEST}")
                _TEST_DATA_DIR_CONFTEST = None
                break
            except Exception as e:
                logger.warning(f"CONFTEST: Error removing test data dir (Attempt {i+1}): {e}")
                if i < 2:
                    time.sleep(0.5) # Pausa tra i tentativi
                else:
                    logger.error(f"CONFTEST: Failed to remove test dir after multiple attempts: {_TEST_DATA_DIR_CONFTEST}")
    
@pytest.fixture(scope='function') # 'function' scope è corretto per client per isolamento
def client(app): # Ora dipende dalla fixture 'app' definita sopra
    return app.test_client()
