import pytest
import sqlite3
from unittest.mock import patch, MagicMock
from flask import url_for

# La funzione helper rimane identica
def setup_wordpress_user(client, app, email="wp_user_unique@example.com", password="password"):
    client.post(url_for('register'), data={ 'name': 'WP Test User', 'email': email, 'password': password, 'confirm_password': password }, follow_redirects=True)
    client.post(url_for('login'), data={'email': email, 'password': password}, follow_redirects=True)
    user_id = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user_row = cursor.fetchone()
        if user_row:
            user_id = user_row[0]
            cursor.execute("""
                INSERT INTO user_settings (user_id, wordpress_url, wordpress_username, wordpress_api_key)
                VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                    wordpress_url = excluded.wordpress_url,
                    wordpress_username = excluded.wordpress_username,
                    wordpress_api_key = excluded.wordpress_api_key
            """, (user_id, "https://fakesite.com", "fake_user", "fake_password"))
            conn.commit()
        conn.close()
    return user_id

# Il nostro test principale
def test_wordpress_sync_logic(client, app, monkeypatch):
    """
    Testa la logica di sincronizzazione di WordPress, mockando TUTTE le dipendenze lente.
    """
    email_per_il_test = "wp_user_unique@example.com"
    monkeypatch.setenv("ALLOWED_EMAILS", email_per_il_test)

    # 1. ARRANGE
    user_id = setup_wordpress_user(client, app, email=email_per_il_test)
    assert user_id is not None, "La preparazione dell'utente di test è fallita."

    mock_wp_posts = [{'id': 1, 'link': 'https://fakesite.com/post1', 'title': {'rendered': 'Articolo di Test 1'}, 'content': {'rendered': '<p>Questo è un articolo di test abbastanza lungo da superare la soglia minima di parole. Serve a verificare che la logica di sincronizzazione di WordPress funzioni correttamente, senza essere filtrato come contenuto troppo breve. Il nostro sistema deve poter processare articoli nuovi e aggiornati, e questa frase serve proprio a questo scopo. Ora dovremmo avere più di 50 parole.</p>'}, 'modified_gmt': '2023-01-01T10:00:00'}]    
    mock_wp_pages = [{
        'id': 101,
        'link': 'https://fakesite.com/pagina1',
        'title': {'rendered': 'Pagina di Test 1'},
        'content': {
            'rendered': '<p>Questo è il contenuto della pagina di test, scritto in modo sufficientemente lungo da superare la soglia minima di parole imposta nel codice. Serve a verificare che la pagina venga indicizzata e non venga scartata perché troppo breve. Contiene frasi aggiuntive per raggiungere il numero di parole richiesto e per simulare un vero contenuto di pagina statico. Include anche altre frasi che servono a testare il comportamento del sync.</p>'
        },
        'modified_gmt': '2023-01-02T11:00:00'
    }]


    mock_wp_client_instance = MagicMock()
    mock_wp_client_instance.get_all_posts.return_value = mock_wp_posts
    mock_wp_client_instance.get_all_pages.return_value = mock_wp_pages
    
    # Definiamo i path di TUTTE le funzioni esterne O LENTE che dobbiamo "ingannare"
    path_to_wp_client = 'app.api.routes.website.WordPressClient'
    path_to_index_article = 'app.api.routes.website._index_article' # <-- NUOVO PATCH
    path_to_index_page = 'app.api.routes.website._index_page'       # <-- NUOVO PATCH

    with patch(path_to_wp_client, return_value=mock_wp_client_instance) as mock_wp_client_class, \
         patch(path_to_index_article, return_value='completed') as mock_index_article_func, \
         patch(path_to_index_page, return_value='completed') as mock_index_page_func:
        
        # 2. ACT
        from app.api.routes.website import _background_wp_sync_core
        
        with app.app_context():
            settings = {}
            conn = sqlite3.connect(app.config['DATABASE_FILE'])
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT wordpress_url, wordpress_username, wordpress_api_key FROM user_settings WHERE user_id = ?", (user_id,))
            settings_row = cursor.fetchone()
            if settings_row:
                settings = dict(settings_row)
            conn.close()

            # Creiamo un finto core_config per il test
            fake_core_config = {'BASE_DIR': app.config.get('BASE_DIR')}
            _background_wp_sync_core(app.app_context(), user_id, settings, fake_core_config)

        # 3. ASSERT
        # Verifichiamo che le funzioni di indicizzazione siano state CHIAMATE
        mock_index_article_func.assert_called_once()
        mock_index_page_func.assert_called_once()
        
        # E verifichiamo comunque che i dati base siano stati inseriti nel DB
        # (perché questo lo fa _background_wp_sync_core prima di chiamare l'indicizzazione)
        with app.app_context():
            conn = sqlite3.connect(app.config['DATABASE_FILE'])
            cursor = conn.cursor()
            
            cursor.execute("SELECT title, user_id FROM articles WHERE article_url = ?", ('https://fakesite.com/post1',))
            article_row = cursor.fetchone()
            assert article_row is not None
            assert article_row[0] == 'Articolo di Test 1'

            cursor.execute("SELECT title, user_id FROM pages WHERE page_url = ?", ('https://fakesite.com/pagina1',))
            page_row = cursor.fetchone()
            assert page_row is not None
            assert page_row[0] == 'Pagina di Test 1'
            
            conn.close()