import pytest
from unittest.mock import patch, MagicMock
from flask import url_for
import sqlite3

# Importiamo la funzione che vogliamo testare
from app.core.system_info import get_system_stats

# Funzione helper (già corretta)
def login_test_user_for_stats(client, app, monkeypatch):
    email = "stats_user@test.com"
    password = "password"
    monkeypatch.setenv("ALLOWED_EMAILS", email)
    
    client.post(url_for('register'), data={'email': email, 'password': password, 'confirm_password': password})
    client.post(url_for('login'), data={'email': email, 'password': password})

def test_get_system_stats_runs_without_crashing(client, app, monkeypatch):
    """
    SMOKE TEST: Verifica che get_system_stats() venga eseguita senza errori
    e restituisca la struttura di base, anche con servizi mockati.
    Test abbastanza inutile dal mio punto di vista.
    """
    # 1. ARRANGE
    login_test_user_for_stats(client, app, monkeypatch)

    # Spia SEMPLICE per lo scheduler: simuliamo solo che esista.
    mock_scheduler = MagicMock()
    mock_scheduler.get_jobs.return_value = [] # Simuliamo che non ci siano job, va benissimo.
    app.config['SCHEDULER_INSTANCE'] = mock_scheduler

    # Spia SEMPLICE per PSUtil
    path_psutil = 'app.core.system_info.psutil'
    with patch(path_psutil, MagicMock()):
        
        # 2. ACT
        with client:
            # Eseguiamo la funzione e ci assicuriamo che non generi un'eccezione
            try:
                system_stats = get_system_stats()
                execution_error = None
            except Exception as e:
                system_stats = None
                execution_error = e

    # 3. ASSERT
    # La funzione non deve crashare
    assert execution_error is None, f"get_system_stats ha generato un errore inaspettato: {execution_error}"
    
    # Il risultato deve essere un dizionario
    assert isinstance(system_stats, dict)
    
    # Verifichiamo che le chiavi principali siano presenti
    assert 'db_status' in system_stats
    assert 'scheduler_status' in system_stats
    assert 'ram_status' in system_stats
    assert 'version_status' in system_stats