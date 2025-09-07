import pytest
from flask import session, url_for
from app.models.user import User # Per verificare l'hashing delle password se necessario, o per istanziare
                                 # ma di solito non è necessario per i test di integrazione delle route.

# --- Test Registrazione ---

def test_register_page_loads(client):
    """Verifica che la pagina di registrazione venga caricata correttamente."""
    response = client.get(url_for('register'))
    assert response.status_code == 200
    assert b"Registra un Nuovo Account" in response.data # Controlla un testo specifico della pagina

def test_successful_registration(client, app): # Usa la fixture 'app'
    """Testa la registrazione di un nuovo utente con successo."""
    user_data = {
        'name': 'Test User',
        'email': 'test@example.com',
        'password': 'password123',
        'confirm_password': 'password123'
    }
    response = client.post(url_for('register'), data=user_data, follow_redirects=True)

    assert response.status_code == 200
    assert b"Registrazione completata! Ora puoi effettuare il login." in response.data

    with app.app_context(): # Usa 'app' qui
        import sqlite3
        conn = sqlite3.connect(app.config['DATABASE_FILE']) # Usa app.config
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE email = ?", (user_data['email'],))
        db_user = cursor.fetchone()
        conn.close()
        assert db_user is not None
        assert db_user[0] == user_data['email']

def test_registration_email_already_exists(client):
    """Testa la registrazione con un'email già esistente."""
    # Prima registra un utente
    client.post(url_for('register'), data={
        'name': 'Existing User',
        'email': 'existing@example.com',
        'password': 'password123',
        'confirm_password': 'password123'
    }, follow_redirects=True)

    # Prova a registrare di nuovo con la stessa email
    response = client.post(url_for('register'), data={
        'name': 'Another User',
        'email': 'existing@example.com', # Stessa email
        'password': 'newpassword',
        'confirm_password': 'newpassword'
    }, follow_redirects=True)

    assert response.status_code == 200 # Rimane sulla pagina di registrazione (o redirect a register)
    assert b"Email gi\xc3\xa0 registrata. Effettua il login." in response.data # \xc3\xa0 è 'à' in UTF-8

def test_registration_password_mismatch(client):
    """Testa la registrazione con password non corrispondenti."""
    response = client.post(url_for('register'), data={
        'name': 'Mismatch User',
        'email': 'mismatch@example.com',
        'password': 'password123',
        'confirm_password': 'password456' # Password diverse
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Le password non coincidono." in response.data

def test_registration_missing_fields(client):
    """Testa la registrazione con campi mancanti."""
    # Manca email
    response_no_email = client.post(url_for('register'), data={
        'name': 'No Email User',
        'password': 'password123',
        'confirm_password': 'password123'
    }, follow_redirects=True)
    assert b"Email, password e conferma password sono richiesti." in response_no_email.data # O un messaggio più specifico se la validazione è per campo

    # Manca password
    response_no_pass = client.post(url_for('register'), data={
        'name': 'No Pass User',
        'email': 'nopass@example.com',
        'confirm_password': 'password123'
    }, follow_redirects=True)
    assert b"Email, password e conferma password sono richiesti." in response_no_pass.data


# --- Test Login ---

def test_login_page_loads(client):
    """Verifica che la pagina di login venga caricata correttamente."""
    response = client.get(url_for('login'))
    assert response.status_code == 200
    assert b"Login" in response.data # Controlla il titolo o un testo specifico

# Helper function per registrare e poi loggare un utente per altri test
def register_and_login_user(client, email, password, name="Test User"):
    client.post(url_for('register'), data={
        'name': name,
        'email': email,
        'password': password,
        'confirm_password': password
    }, follow_redirects=True)

    response = client.post(url_for('login'), data={
        'email': email,
        'password': password
    }, follow_redirects=True)
    return response

def test_successful_login(client):
    """Testa il login di un utente registrato con successo."""
    email = 'login_success@example.com'
    password = 'password123'
    response = register_and_login_user(client, email, password) # Usa l'helper

    assert response.status_code == 200
    # Dopo il login con successo, ci si aspetta un redirect alla ingresso_dati
    # Il contenuto della ingresso_dati potrebbe variare, quindi controlliamo l'URL o un messaggio specifico
    # Se '/ingresso_dati' è il target, e `follow_redirects=True`, response.request.path dovrebbe essere '/ingresso_dati'
    # o controlla un testo che sai essere sulla ingresso_dati
    assert b"ingresso_dati" in response.data # Assumendo che "ingresso_dati" sia nel titolo o h1 della ingresso_dati
    # Verifica che l'utente sia in sessione
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is not None


def test_login_wrong_credentials(client):
    """Testa il login con credenziali errate."""
    email = 'wrong_creds@example.com'
    password = 'password123'
    # Registra l'utente prima
    client.post(url_for('register'), data={
        'name': 'Wrong Creds User',
        'email': email,
        'password': password,
        'confirm_password': password
    }, follow_redirects=True)

    # Tenta il login con password sbagliata
    response = client.post(url_for('login'), data={
        'email': email,
        'password': 'wrongpassword'
    }, follow_redirects=True)
    assert response.status_code == 200 # Rimane sulla pagina di login (o redirect a login)
    assert b"Email o password non validi." in response.data
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is None # Nessun utente in sessione

def test_login_non_existent_user(client):
    """Testa il login con un utente che non esiste."""
    response = client.post(url_for('login'), data={
        'email': 'nonexistent@example.com',
        'password': 'password123'
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Email o password non validi." in response.data # Stesso messaggio per sicurezza
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is None

# --- Test Logout ---

def test_logout(client):
    """Testa il logout di un utente."""
    email = 'logout_user@example.com'
    password = 'password123'
    register_and_login_user(client, email, password) # Registra e logga

    # Verifica che l'utente sia loggato prima del logout
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is not None

    # Effettua il logout
    response = client.get(url_for('logout'), follow_redirects=True)
    assert response.status_code == 200
    assert b"Login" in response.data # Reindirizzato alla pagina di login
    assert b"Sei stato disconnesso." in response.data # Messaggio flash di logout

    # Verifica che l'utente non sia più in sessione
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is None

# --- Test Accesso Pagine Protette ---

def test_access_protected_page_unauthenticated(client):
    """Testa l'accesso a una pagina protetta da utente non autenticato."""
    response = client.get(url_for('ingresso_dati'), follow_redirects=True)
    assert response.status_code == 200
    assert b"Login" in response.data # Reindirizzato al login
    assert b"Per favore, effettua il login per accedere a questa pagina." in response.data

def test_access_protected_page_authenticated(client):
    """Testa l'accesso a una pagina protetta da utente autenticato."""
    email = 'authed_user@example.com'
    password = 'password123'
    register_and_login_user(client, email, password) # Registra e logga

    response = client.get(url_for('ingresso_dati')) # Non seguire i redirect qui, vogliamo la risposta diretta
    assert response.status_code == 200
    assert b"ingresso_dati" in response.data # Dovrebbe mostrare la ingresso_dati


