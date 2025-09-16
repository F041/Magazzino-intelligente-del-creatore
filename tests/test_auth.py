import pytest
from flask import session, url_for
from app.models.user import User

# --- Test Registrazione ---

def test_register_page_loads(client):
    """Verifica che la pagina di registrazione venga caricata correttamente."""
    response = client.get(url_for('register'))
    assert response.status_code == 200
    assert b"Registra un Nuovo Account" in response.data

def test_successful_registration(client, app, monkeypatch):
    """Testa la registrazione di un nuovo utente con successo."""
    email_to_test = 'test@example.com'
    # Usiamo monkeypatch per impostare l'email permessa SOLO per questo test
    monkeypatch.setenv("ALLOWED_EMAILS", email_to_test)

    user_data = {
        'name': 'Test User',
        'email': email_to_test,
        'password': 'password123',
        'confirm_password': 'password123'
    }
    response = client.post(url_for('register'), data=user_data, follow_redirects=True)

    assert response.status_code == 200
    assert b"Registrazione completata! Ora puoi effettuare il login." in response.data

    # Verifica che l'utente sia stato effettivamente salvato nel database
    with app.app_context():
        import sqlite3
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE email = ?", (user_data['email'],))
        db_user = cursor.fetchone()
        conn.close()
        assert db_user is not None
        assert db_user[0] == user_data['email']

def test_registration_email_already_exists(client, monkeypatch):
    """Testa la registrazione con un'email già esistente."""
    email_to_test = 'existing@example.com'
    monkeypatch.setenv("ALLOWED_EMAILS", email_to_test)

    # Prima registra un utente
    client.post(url_for('register'), data={
        'name': 'Existing User',
        'email': email_to_test,
        'password': 'password123',
        'confirm_password': 'password123'
    }, follow_redirects=True)

    # Prova a registrare di nuovo con la stessa email
    response = client.post(url_for('register'), data={
        'name': 'Another User',
        'email': email_to_test,
        'password': 'newpassword',
        'confirm_password': 'newpassword'
    }, follow_redirects=True)

    assert response.status_code == 200
    assert b"Email gi\xc3\xa0 registrata. Effettua il login." in response.data

def test_registration_password_mismatch(client, monkeypatch):
    """Testa la registrazione con password non corrispondenti."""
    email_to_test = 'mismatch@example.com'
    monkeypatch.setenv("ALLOWED_EMAILS", email_to_test)

    response = client.post(url_for('register'), data={
        'name': 'Mismatch User',
        'email': email_to_test,
        'password': 'password123',
        'confirm_password': 'password456'
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Le password non coincidono." in response.data

def test_registration_with_unallowed_email(client,monkeypatch):
    """Testa che la registrazione fallisca se l'email non è in lista (quando la lista esiste)."""
    # Impostiamo una lista che NON include l'email di test
    monkeypatch.setenv("ALLOWED_EMAILS", "allowed@example.com")
    
    response = client.post(url_for('register'), data={
        'name': 'Unallowed User',
        'email': 'unallowed@example.com',
        'password': 'password123',
        'confirm_password': 'password123'
    }, follow_redirects=True)
    
    assert response.status_code == 200
    # Controlla che il messaggio di errore di default (o quello custom) sia presente
    assert b"Non sei autorizzato a registrare un account." in response.data

# --- Test Login ---

def test_login_page_loads(client):
    """Verifica che la pagina di login venga caricata correttamente."""
    response = client.get(url_for('login'))
    assert response.status_code == 200
    assert b"Accedi" in response.data

def test_successful_login(client, app, monkeypatch):
    """Testa il login di un utente registrato con successo."""
    email = 'login_success@example.com'
    password = 'password123'
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'name': 'Login User', 'email': email, 'password': password, 'confirm_password': password})

    response = client.post(url_for('login'), data={'email': email, 'password': password}, follow_redirects=True)
    
    assert response.status_code == 200
    # CORREZIONE: L'endpoint si chiama 'data_entry', non 'ingresso_dati'
    assert response.request.path == url_for('data_entry')
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is not None

def test_logout(client, app, monkeypatch):
    """Testa il logout di un utente."""
    email = 'logout_user@example.com'
    password = 'password123'
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'name': 'Logout User', 'email': email, 'password': password, 'confirm_password': password})
    client.post(url_for('login'), data={'email': email, 'password': password})

    with client.session_transaction() as sess:
        assert sess.get('_user_id') is not None

    response = client.get(url_for('logout'), follow_redirects=True)
    assert response.status_code == 200
    assert b"Sei stato disconnesso." in response.data
    with client.session_transaction() as sess:
        assert sess.get('_user_id') is None

# --- Test Accesso Pagine Protette ---

def test_access_protected_page_unauthenticated(client):
    """Testa l'accesso a una pagina protetta da utente non autenticato."""
    # CORREZIONE: L'endpoint si chiama 'data_entry'
    response = client.get(url_for('data_entry'), follow_redirects=True)
    assert response.status_code == 200
    assert b"Per favore, effettua il login per accedere a questa pagina." in response.data

def test_access_protected_page_authenticated(client, monkeypatch):
    """Testa l'accesso a una pagina protetta da utente autenticato."""
    email = 'authed_user@example.com'
    password = 'password123'
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    client.post(url_for('register'), data={'name': 'Authed User', 'email': email, 'password': password, 'confirm_password': password})
    client.post(url_for('login'), data={'email': email, 'password': password})

    # CORREZIONE: L'endpoint si chiama 'data_entry'
    response = client.get(url_for('data_entry'))
    assert response.status_code == 200
    assert b"Ingresso dati" in response.data