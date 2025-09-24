import pytest
import os
import subprocess
import time
import sqlite3
import requests
from requests.exceptions import ConnectionError

# --- Configurazione del Test ---
# L'URL del canale YouTube pubblico che vogliamo usare per il test.
# Usiamo il tuo, come concordato.
TEST_CHANNEL_URL = "https://www.youtube.com/channel/UCJSuTw2VDoX0CWejniSo5TA/" 
# L'indirizzo base a cui risponderà la nostra applicazione Docker.
BASE_URL = "http://localhost:5000" 
# Email e password per l'utente di test che creeremo.
TEST_USER_EMAIL = "e2e_test_user@example.com"
TEST_USER_PASSWORD = "a_secure_password_123"

# --- Funzioni di aiuto (Fixture di Pytest) ---

@pytest.fixture(scope="function")
def app_environment():
    """
    Una fixture di Pytest che gestisce l'intero ciclo di vita dell'ambiente di test:
    1. Pulisce i dati vecchi.
    2. Avvia i container Docker.
    3. Fornisce gli strumenti per interagire con l'app.
    4. Alla fine, ferma e pulisce tutto.
    """
    
    # --- Blocco di pulizia invariato ---
    print("\n--- [E2E Setup] Pulizia ambiente precedente ---")
    subprocess.run(["docker-compose", "-f", "docker-compose.test.yml", "down", "-v"], check=True)
    
    # --- Blocco di avvio invariato ---
    print("--- [E2E Setup] Avvio dei container Docker con --build ---")
    subprocess.run(["docker-compose", "-f", "docker-compose.test.yml", "up", "--build", "-d"], check=True)

    # --- INIZIO BLOCCO MODIFICATO ---
    print("--- [E2E Setup] Attesa che il servizio Flask sia raggiungibile e pronto ---")
    session = requests.Session()
    retries = 20 # Aumentiamo i tentativi a 20 (40 secondi totali)
    for i in range(retries):
        try:
            # Non ci limitiamo a chiedere la home, proviamo a fare il login.
            # Se fallisce, l'app potrebbe non essere ancora pronta.
            # Usiamo dati palesemente sbagliati per testare solo la raggiungibilità della logica di login.
            ping_response = session.get(f"{BASE_URL}/login")
            if ping_response.status_code == 200:
                print(f"--- [E2E Setup] L'applicazione ha risposto alla richiesta di login al tentativo #{i+1}. Procedo. ---")
                break
        except ConnectionError:
            print(f"Tentativo #{i+1} fallito: Connessione rifiutata. Riprovo tra 2 secondi...")
            time.sleep(2)
    else:
        # Se il ciclo finisce, mostriamo i log del container per capire perché non è partito.
        logs = subprocess.check_output(["docker-compose", "-f", "docker-compose.test.yml", "logs", "app"])
        pytest.fail(f"L'applicazione Docker non è diventata raggiungibile in tempo utile.\nLogs:\n{logs.decode('utf-8')}")

    # 4. Creazione dell'utente e Login (con più controlli)
    try:
        print(f"--- [E2E Setup] Tentativo di registrazione per {TEST_USER_EMAIL}... ---")
        reg_response = session.post(f"{BASE_URL}/register", data={'email': TEST_USER_EMAIL, 'password': TEST_USER_PASSWORD, 'confirm_password': TEST_USER_PASSWORD}, allow_redirects=True)
        # Un successo nella registrazione ci porta alla pagina di login con un messaggio
        if "Registrazione completata!" not in reg_response.text:
             pytest.fail(f"La registrazione sembra essere fallita. Contenuto pagina:\n{reg_response.text[:500]}")
        print("--- [E2E Setup] Registrazione riuscita. ---")
        
        print(f"--- [E2E Setup] Tentativo di login per {TEST_USER_EMAIL}... ---")
        login_resp = session.post(f"{BASE_URL}/login", data={'email': TEST_USER_EMAIL, 'password': TEST_USER_PASSWORD}, allow_redirects=True)
        
        # Dopo un login corretto, NON dovremmo più essere sulla pagina di login
        if "/login" in login_resp.url:
             pytest.fail(f"Login fallito durante il setup del test E2E. L'URL finale è ancora /login. Contenuto pagina:\n{login_resp.text[:500]}")
        
        print(f"--- [E2E Setup] Utente {TEST_USER_EMAIL} registrato e loggato con successo ---")
    except Exception as e:
        logs = subprocess.check_output(["docker-compose", "-f", "docker-compose.test.yml", "logs", "app"])
        pytest.fail(f"Errore durante la creazione/login dell'utente di test: {e}\nLogs:\n{logs.decode('utf-8')}")
    # --- FINE BLOCCO MODIFICATO ---

    yield session

    # --- Blocco di pulizia invariato ---
    print("\n--- [E2E Teardown] Arresto dei container ---")
    subprocess.run(["docker-compose", "-f", "docker-compose.test.yml", "down", "-v"], check=True)

# --- Il nostro Test E2E ---

@pytest.mark.skip(reason="Disattivato temporaneamente per problemi di configurazione ambiente in Docker.")
def test_full_scheduler_flow_from_monitoring_setup(app_environment):
    """
    Testa l'intero flusso: 
    1. Imposta il monitoraggio di un canale via API.
    2. Attiva lo scheduler.
    3. Attende il completamento.
    4. Verifica i risultati nel database.
    """
    # La fixture 'app_environment' ci fornisce una sessione già loggata
    session = app_environment

    # Per ora, fermiamoci qui.
    # Al prossimo passo aggiungeremo la logica di Azione e Verifica.
    assert session is not None