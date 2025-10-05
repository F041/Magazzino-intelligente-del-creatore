import pytest
from app.api.routes.search import build_prompt

def test_build_prompt_with_context():
    """
    TEST SCENARIO 1: Verifica che il prompt includa correttamente
    la domanda dell'utente e i pezzi di contesto forniti.
    """
    # 1. ARRANGE: Prepariamo i dati di input
    query = "Qual è il colore del cavallo bianco di Napoleone?"
    context_chunks = [
        {'text': 'Napoleone aveva un cavallo bianco.'},
        {'text': 'Il colore del cavallo era, appunto, bianco.'}
    ]
    history = []

    # 2. ACT: Eseguiamo la funzione
    prompt = build_prompt(query, context_chunks, history)

    # 3. ASSERT: Verifichiamo che gli elementi chiave siano nel prompt finale
    assert query in prompt
    assert "Napoleone aveva un cavallo bianco." in prompt
    assert "Il colore del cavallo era, appunto, bianco." in prompt
    # Verifichiamo anche che NON contenga il messaggio di fallback
    assert "non ho trovato informazioni pertinenti" not in prompt.lower()

def test_build_prompt_without_context_uses_fallback():
    """
    TEST SCENARIO 2: Verifica che, in assenza di contesto,
    il prompt istruisca l'IA a specificare che non ha trovato informazioni.
    """
    # 1. ARRANGE
    query = "Di cosa parlano i documenti?"
    context_chunks = [] # <-- La differenza chiave è qui: lista vuota
    history = []

    # 2. ACT
    prompt = build_prompt(query, context_chunks, history)

    # 3. ASSERT
    assert query in prompt
    # Verifichiamo che contenga la frase di fallback
    assert "specifica che non hai trovato informazioni pertinenti" in prompt