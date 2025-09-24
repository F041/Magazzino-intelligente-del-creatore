import os
import tempfile
import shutil
from app.config import TestConfig
from app.main import create_app


def test_index_page_loads(client): # client ora viene da conftest.py
    response = client.get('/')
    assert response.status_code in [200, 302]

import os
import tempfile
import shutil
from app.config import TestConfig
from app.main import create_app

def test_init_db_creates_database_file_after_refactor():
    """
    Verifica che la creazione dell'app chiami init_db e crei il file del database,
    e che la pulizia finale funzioni correttamente.
    """
    test_dir = tempfile.mkdtemp()
    app = None # Definiamo app qui fuori dal try per averla nel finally
    
    try:
        # 1. Configura
        test_config_instance = TestConfig()
        test_config_instance._TEST_BASE_DIR = test_dir
        
        client_secrets_path = test_config_instance.CLIENT_SECRETS_PATH
        with open(client_secrets_path, 'w') as f:
            f.write('{"installed": {}}')
            
        # 2. Esegui
        app = create_app(test_config_instance)

        # 3. Verifica
        db_path = app.config['DATABASE_FILE']
        assert test_dir in db_path 
        assert os.path.exists(db_path)
        
    finally:
        # --- BLOCCO DI PULIZIA MIGLIORATO ---
        # 4. Spegni ChromaDB PRIMA della pulizia, se l'app è stata creata
        if app:
            chroma_client = app.config.get('CHROMA_CLIENT')
            if chroma_client and hasattr(chroma_client, '_system') and hasattr(chroma_client._system, 'stop'):
                try:
                    chroma_client._system.stop()
                except Exception as e:
                    print(f"Warning: error stopping chroma during teardown: {e}") # Usiamo print qui perché il logger potrebbe essere già stato smantellato

        # 5. La pulizia ora avviene nel blocco finally
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)