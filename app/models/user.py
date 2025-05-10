from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import uuid # Per generare ID utente univoci

class User(UserMixin):
    """Modello utente per Flask-Login."""

    def __init__(self, id, email, password_hash=None, name=None):
        self.id = id # Deve essere una stringa univoca (useremo UUID)
        self.email = email
        self.password_hash = password_hash
        self.name = name # Opzionale: nome visualizzato

    def set_password(self, password):
        """Genera l'hash della password e lo memorizza."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verifica se la password fornita corrisponde all'hash memorizzato."""
        if not self.password_hash:
            return False # Nessuna password impostata
        return check_password_hash(self.password_hash, password)

    @staticmethod
    def generate_id():
        """Genera un ID utente univoco."""
        return str(uuid.uuid4())

    # Metodi richiesti da UserMixin (anche se __init__ imposta già self.id)
    # Flask-Login usa get_id() per memorizzare l'ID nella sessione.
    def get_id(self):
        return str(self.id)

    # __repr__ è utile per il debug
    def __repr__(self):
        return f'<User {self.email} (ID: {self.id})>'