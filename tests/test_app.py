def test_index_page_loads(client): # client ora viene da conftest.py
    response = client.get('/')
    assert response.status_code in [200, 302]
