
import secrets
import string

def generate_api_key(length=40):
    """Genera una chiave API sicura e casuale."""
    # Usa lettere maiuscole/minuscole, numeri. Escludi caratteri ambigui se vuoi.
    alphabet = string.ascii_letters + string.digits
    # Aggiungi un prefisso per riconoscibilità (opzionale)
    prefix = "sk_" # Simula "secret key"
    # Calcola lunghezza parte casuale
    random_length = length - len(prefix)
    if random_length <= 0:
        raise ValueError("La lunghezza richiesta per la chiave API è troppo corta.")
    # Genera parte casuale
    random_part = ''.join(secrets.choice(alphabet) for _ in range(random_length))
    return prefix + random_part

# aggiungerò qui altre funzioni di utilità in futuro