# Usa un'immagine Python ufficiale. Scegli la versione che stai usando.
# Usare un'immagine "slim" riduce la dimensione finale.
FROM python:3.9-slim
ENV TZ=Europe/Rome
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Imposta la directory di lavoro nell'immagine
WORKDIR /app

# Imposta variabili d'ambiente (PYTHONUNBUFFERED è utile per i log Docker)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Installa git, che ci serve per scrivere la versione.
# apt-get update aggiorna la lista dei pacchetti
# --no-install-recommends installa solo il minimo indispensabile
# apt-get clean e rm -rf /var/lib/apt/lists/* puliscono per mantenere l'immagine leggera
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    sqlite3 \
    nano \
    && rm -rf /var/lib/apt/lists/*

# Copia prima il file requirements.txt, la "ricetta"
COPY requirements.txt .

# Installa le dipendenze Python direttamente nell'immagine runtime
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    echo "Tentativo rimozione dipendenze pesanti post-installazione (versione 2)..." && \
    pip uninstall -y onnxruntime sympy huggingface-hub hf-xet kubernetes fastapi uvicorn typer rich && \
    rm -rf /root/.cache/pip

# Copia il resto dell'applicazione
COPY . .

# Scrive l'hash del commit Git in un file per il versioning.
# Questo deve stare DOPO "COPY . ." per avere accesso alla cartella .git
RUN git rev-parse --short HEAD > version.txt

ENV FLASK_RUN_PORT 5000
ENV FLASK_RUN_HOST 0.0.0.0
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "app.main:create_app()"]