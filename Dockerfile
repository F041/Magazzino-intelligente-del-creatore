# Usa un'immagine Python ufficiale. Scegli la versione che stai usando.
# Usare un'immagine "slim" riduce la dimensione finale.
FROM python:3.9-slim

# Imposta la directory di lavoro nell'immagine
WORKDIR /app

# Imposta variabili d'ambiente (PYTHONUNBUFFERED è utile per i log Docker)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Copia prima il file requirements.txt, la "ricetta"
COPY requirements.txt .

# Installa le dipendenze Python direttamente nell'immagine runtime

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    echo "Tentativo rimozione dipendenze pesanti post-installazione (versione 2)..." && \
    # Rimuoviamo onnxruntime, sympy, huggingface, hf-xet, kubernetes, fastapi, uvicorn, typer, rich
    # MA LASCIAMO opentelemetry e le sue sotto-dipendenze perché sembrano necessarie per l'import di chromadb
    pip uninstall -y onnxruntime sympy huggingface-hub hf-xet kubernetes fastapi uvicorn typer rich && \
    rm -rf /root/.cache/pip

# Copia il resto dell'applicazione
COPY . .

ENV FLASK_RUN_PORT 5000
ENV FLASK_RUN_HOST 0.0.0.0
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "app.main:create_app()"]
