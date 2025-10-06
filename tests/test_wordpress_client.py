import pytest
from unittest.mock import patch, MagicMock
from app.services.wordpress.client import WordPressClient

# Definiamo il percorso della libreria 'requests' che vogliamo "ingannare"
REQUESTS_GET_PATH = 'app.services.wordpress.client.requests.get'

def test_wordpress_client_handles_pagination(monkeypatch):
    """
    TEST: Verifica che il WordPressClient gestisca correttamente la paginazione
    dell'API REST di WordPress. (Versione Corretta)
    """
    # 1. ARRANGE
    mock_response_page1 = MagicMock()
    mock_response_page1.status_code = 200
    mock_response_page1.headers = {'X-WP-TotalPages': '2'}
    mock_response_page1.json.return_value = [
        {'id': 1, 'title': {'rendered': 'Articolo Pagina 1'}}
    ]

    mock_response_page2 = MagicMock()
    mock_response_page2.status_code = 200
    mock_response_page2.headers = {'X-WP-TotalPages': '2'}
    mock_response_page2.json.return_value = [
        {'id': 2, 'title': {'rendered': 'Articolo Pagina 2'}}
    ]

    with patch(REQUESTS_GET_PATH) as mock_get:
        # Il side_effect ora contiene solo le 2 risposte attese
        mock_get.side_effect = [
            mock_response_page1, 
            mock_response_page2
        ]

        # 2. ACT
        client = WordPressClient(
            site_url="https://fakesite.com",
            username="fake_user",
            app_password="fake_password"
        )
        all_posts = client.get_all_posts()

    # 3. ASSERT
    assert len(all_posts) == 2
    assert all_posts[0]['id'] == 1
    assert all_posts[1]['id'] == 2

    # L'asserzione corretta è 2
    assert mock_get.call_count == 2