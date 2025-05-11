![Main application page](screenshots/main.png)

# Magazzino del Creatore

Un'applicazione per creare una base di conoscenza interrogabile a partire dai contenuti di un creator (video YouTube, documenti, articoli RSS), utilizzando Flask per il backend, ChromaDB come database vettoriale e Google Gemini per embedding e generazione di risposte (architettura RAG).

**Modalità Operative (`APP_MODE`)**

L'applicazione supporta due modalità operative principali, configurabili tramite la variabile `APP_MODE` nel file `.env`:

*   **`APP_MODE=single`:**
    *   Ideale per uso personale o singolo creator.
    *   Tutti i dati (SQLite e ChromaDB) sono condivisi in un unico set di tabelle/collezioni.
    *   Non richiede autenticazione utente specifica per l'accesso ai dati (solo l'autenticazione Google iniziale per le API).
*   **`APP_MODE=saas`:**
    *   Progettata per supportare potenzialmente più utenti con dati isolati.
    *   **Attualmente implementata con autenticazione utente basata su Email/Password via Flask-Login** per l'interfaccia web Flask.
    *   I dati in SQLite (video, documenti, articoli) sono associati a un `user_id`.
    *   Le collezioni in ChromaDB vengono create dinamicamente per ogni utente (es. `video_transcripts_userid`).
    *   L'accesso alle API richiede un'**API Key** specifica dell'utente.

**Stato Attuale Modalità `saas` (Importante):**
*   L'autenticazione utente Flask (registrazione/login) è funzionante.
*   L'isolamento dei dati in SQLite e ChromaDB per utente è implementato.
*   È stato aggiunto un sistema di gestione delle **API Key** (generazione/eliminazione) tramite l'interfaccia Flask (`/keys/manage`).
*   L'API di ricerca (`/api/search/`) ora richiede una `X-API-Key` valida nell'header per autenticare le richieste provenienti da applicazioni esterne (come Telegram) e associarle all'utente corretto.


## Caratteristiche Principali

*   **Autenticazione Utente (Modalità `saas`):**
    *   Registrazione e Login sicuri basati su Email/Password (Flask-Login).
    *   Gestione sessioni utente per l'interfaccia web Flask.
*   **Gestione API Key (Modalità `saas`):**
    *   Interfaccia web (`/keys/manage`) per utenti loggati per generare, visualizzare ed eliminare chiavi API personali.
    *   Le API critiche (es. `/api/search/`) sono protette e richiedono una chiave API valida per identificare l'utente.
*   **Recupero Contenuti YouTube:**
    *   Autenticazione OAuth 2.0 sicura per l'API YouTube (token salvato in `token.pickle`/`.json`).
    *   Recupera metadati e trascrizioni (manuali e automatiche) per i video di un canale specificato.
    *   **Elaborazione Asincrona:** L'elaborazione del canale (recupero, trascrizione, embedding) viene avviata in un thread separato per non bloccare l'interfaccia web.
    *   **Feedback di Avanzamento:** La Dashboard mostra lo stato e l'avanzamento dell'elaborazione del canale interrogando un endpoint API dedicato (`/api/videos/progress`).
*   **Gestione Documenti:**
    *   Upload di file PDF, DOCX, TXT tramite interfaccia web (`/dashboard`).
    *   Conversione automatica in Markdown (`.md`).
    *   Salvataggio file e registrazione nel DB (associato all'utente in `saas`).
    *   Indicizzazione automatica all'upload (chunking, embedding, salvataggio in ChromaDB).
    *   Interfaccia web (`/my-documents`) per visualizzare e **eliminare** documenti (elimina file, record SQLite e chunk da ChromaDB).
*   **Gestione Articoli RSS:**
    *   Parsing di feed RSS/Atom tramite URL.
    *   Estrazione contenuto articoli (con fallback a scraping basico).
    *   Salvataggio contenuto in file `.txt`.
    *   Registrazione nel DB (associato all'utente in `saas`).
    *   Indicizzazione automatica all'aggiunta (chunking, embedding, salvataggio in ChromaDB).
    *   Interfaccia web (`/my-articles`) per visualizzare gli articoli. (Eliminazione da implementare).
*   **Pipeline di Indicizzazione:**
    *   Genera embedding (Google Gemini `text-embedding-004`) per chunk di video, documenti e articoli.
    *   Memorizza metadati e contenuti/trascrizioni in SQLite (con `user_id` in `saas`).
    *   Memorizza embedding vettoriali in **collezioni ChromaDB dedicate per tipo di contenuto e per utente** (in `saas`).
    *   Ottimizzato (per i video) per evitare di riprocessare contenuti già presenti nel DB *per l'utente specifico* (in `saas`).
    *   Pulsante "(Ri)Processa" sempre visibile in `/my-videos` per forzare la re-indicizzazione.
*   **Ricerca Semantica e Generazione (RAG) Multi-Sorgente:**
    *   API `/api/search/` protetta da API Key (in `saas`) o login session (se chiamata da UI Flask).
    *   Identifica l'utente dalla chiave API o dalla sessione.
    *   Genera embedding per la query utente.
    *   Recupera chunk rilevanti dalle **collezioni ChromaDB corrette per l'utente** (video, documenti, articoli).
    *   Ordina i risultati combinati per rilevanza.
    *   Genera una risposta con Google Gemini basata **esclusivamente** sul contesto recuperato.
*   **Interfacce Utente:**
    *   **Backend & Gestione (Flask):** Interfaccia web (`http://localhost:5000`) per:
        *   Registrazione/Login utente (`saas`).
        *   Autenticazione Google OAuth (per API YouTube).
        *   Gestione Chiavi API (`/keys/manage`) (`saas`).
        *   Dashboard (`/dashboard`) per avviare elaborazione Canale YouTube, caricare Documenti, processare Feed RSS, con feedback di avanzamento per i processi asincroni (YouTube, RSS).
        *   Visualizzazione contenuti (`/my-videos`, `/my-documents`, `/my-articles`) con azioni (Riprocessa, Elimina).
        *   Pulsante "Elimina Tutti" per video e articoli (in `/my-videos`, `/my-articles`) (`saas`).
    *   **Chat Interattiva (Flask):**
        *   Pagina dedicata (`/chat`) integrata nell'interfaccia Flask (richiede login).
        *   Permette di interrogare la base di conoscenza (video, documenti, articoli indicizzati per l'utente).
        *   Visualizza risposte generate dall'LLM (Gemini) e i riferimenti utilizzati.
        *   Supporta aggiornamenti di stato intermedi tramite Server-Sent Events (SSE).
    *   **Widget Chat Incorporabile:**
        *   Funzionalità per incorporare la chat su siti web esterni tramite uno snippet JavaScript.
        *   Il creator può ottenere lo snippet dalla pagina `/chat` (richiede incolla di una chiave API dedicata se in modalità `saas`).
        *   L'autenticazione del widget avviene tramite la chiave API fornita nello snippet (modalità `saas`) o è implicita (modalità `single`). (Da rivedere)

## Prerequisiti (per Self-Hosting con Docker)

*   **Docker e Docker Compose:** Necessari per eseguire l'applicazione. Scarica [Docker Desktop](https://www.docker.com/products/docker-desktop/) (include Docker Compose) o installali separatamente sul tuo server Linux.
*   **Git:** Per clonare il repository.
*   **Account Google.**
*   **Progetto Google Cloud:**
    *   Con l'**API YouTube Data v3** abilitata.
    *   Con **Credenziali OAuth 2.0** di tipo "Applicazione Web" create. Dovrai configurare gli URI di reindirizzamento autorizzati (vedi sezione Setup).
    *   Dovrai scaricare il file JSON delle credenziali (solitamente `client_secrets.json`).
*   **API Key di Google AI (Gemini):** Una chiave API valida per utilizzare i modelli Gemini per embedding e generazione.
*   **RAM Consigliata per il Server/Host Docker:**
    *   **Minima (a riposo):** Almeno **250-300 MB** di RAM libera per il container dell'applicazione.
    *   **Consigliata (durante elaborazione):** **500 MB - 1 GB+** per carichi di lavoro più intensi.
    *   *(Nota: Questi valori sono indicativi. Se si utilizza Docker Desktop su Windows, considerare anche la RAM allocata a WSL 2).*
*   **Spazio su Disco Consigliato:**
    *   **Immagine Docker dell'Applicazione:** L'immagine Docker stessa occuperà circa **500-600 MB** dopo la build.
    *   **Dati Utente (directory `./data`):** Questo è **altamente variabile** e dipende dalla quantità e dal tipo di contenuti che indicizzi.
        *   **Database SQLite (`creator_warehouse.db`):** Crescerà con il numero di video, documenti e articoli. Generalmente non enorme a meno di milioni di record.
        *   **Database Vettoriale ChromaDB (`chroma_db/`):** Può diventare **significativo** anche perché pesa almeno almeno **100 volte** il database precedente. La dimensione dipende dal numero di chunk di testo, dalla dimensionalità degli embedding (Gemini `text-embedding-004` ha 768 dimensioni) e dai metadati. Per ogni chunk, memorizzi un vettore di 768 numeri float e metadati.
        *   **File Caricati/Salvati (`uploaded_docs/`, `article_content/`):** La dimensione sarà uguale alla somma dei file Markdown (.md) generati dai documenti e dei file di testo (.txt) degli articoli.
        *   **Token (`token.pickle`):** Trascurabile.
    *   **Raccomandazione Iniziale per i Dati:** Parti con almeno **alcuni GB** di spazio libero per la directory `data` e preparati ad allocarne di più man mano che aggiungi contenuti. Per grandi librerie di contenuti, potresti aver bisogno di decine o centinaia di GB.


## Setup (per Self-Hosting con Docker)

1.  **Clona il Repository:**
    ```bash
    git clone https://github.com/F041/magazzino-creatore.git # Sostituisci con l'URL reale
    cd magazzino-creatore
    ```

2.  **Prepara la Directory dei Dati:**
    Crea una sottodirectory chiamata `data` all'interno della cartella del progetto. Questa directory conterrà tutti i dati persistenti dell'applicazione (database, file caricati, token, ecc.).
    ```bash
    mkdir data
    ```

3.  **Configura le Credenziali OAuth 2.0 di Google:**
    *   **Crea/Configura il tuo Progetto Google Cloud:** Se non l'hai già fatto, vai su [Google Cloud Console](https://console.cloud.google.com/):
        *   Crea un nuovo progetto o selezionane uno esistente.
        *   Abilita l'**API YouTube Data v3** per questo progetto.
        *   Vai a "API e servizi" > "Credenziali".
        *   Crea nuove credenziali di tipo **"ID client OAuth 2.0"**.
        *   Seleziona **"Applicazione web"** come tipo di applicazione.
        *   Dai un nome (es. "Magazzino Creatore SelfHosted").
        *   **URI di reindirizzamento autorizzati:** Questo è un passaggio cruciale. Devi aggiungere l'URL dove la tua istanza di Magazzino del Creatore sarà accessibile, seguito da `/oauth2callback`.
            *   Se esegui Docker localmente per test: `http://localhost:5000/oauth2callback`
            *   Se deployi su un server con un IP o dominio: `http://TUO_IP_O_DOMINIO:5000/oauth2callback` (sostituisci `TUO_IP_O_DOMINIO` e la porta `5000` se la cambi nel `docker-compose.yml`).
            *   È buona norma aggiungere anche `http://127.0.0.1:5000/oauth2callback`.
        *   Clicca su "Crea".
    *   **Scarica il File JSON delle Credenziali:** Dopo la creazione, Google ti mostrerà il tuo ID client e client secret. Clicca sul pulsante di download (icona a forma di freccia verso il basso) per scaricare il file JSON delle credenziali.
    *   **Posiziona e Rinomina il File:**
        *   Rinomina il file JSON scaricato in `client_secrets.json`.
        *   Sposta questo file `client_secrets.json` nella directory `data` che hai creato al passaggio 2 (cioè, deve trovarsi in `./data/client_secrets.json` rispetto alla root del progetto).

4.  **Configura il File d'Ambiente (`.env`):**
    *   Copia il file di esempio `.env.example` in un nuovo file chiamato `.env` nella root del progetto:
        ```bash
        cp .env.example .env
        ```
    *   Apri il file `.env` con un editor di testo e **modifica almeno le seguenti variabili OBBLIGATORIE**:
        *   `FLASK_SECRET_KEY`: Genera una chiave segreta forte e casuale. Puoi usare il comando `python -c 'import secrets; print(secrets.token_hex(32))'` in un terminale Python e copiare l'output.
        *   `GOOGLE_API_KEY`: Inserisci la tua chiave API per Google AI (Gemini).
    *   **Verifica le altre variabili nel `.env`:**
        *   `GOOGLE_CLIENT_SECRETS_FILE` dovrebbe già essere `data/client_secrets.json`.
        *   `APP_MODE`: Imposta a `single` per un uso personale self-hosted (raccomandato), o a `saas` se intendi gestire più account all'interno della tua istanza.
        *   `FLASK_ENV` e `FLASK_DEBUG`: Per produzione, imposta `FLASK_ENV=production` e `FLASK_DEBUG=0`. Per sviluppo/test, puoi usare `development` e `1`.
        *   `OAUTHLIB_INSECURE_TRANSPORT=1`: Lascia `1` se accedi all'app tramite `http://localhost` o un IP senza HTTPS. Se configuri HTTPS, impostalo a `0`.
        *   Gli altri percorsi (`DATABASE_FILE`, `CHROMA_DB_PATH`, ecc.) sono già configurati per funzionare con la directory `data` e Docker.

5.  **(Opzionale - Solo Windows con Docker Desktop) Configura Limiti Risorse WSL 2:**
    Se usi Docker Desktop su Windows e noti un consumo eccessivo di RAM, puoi provare a limitare le risorse per WSL 2 creando o modificando il file `%UserProfile%\.wslconfig` (es. `C:\Users\TuoNome\.wslconfig`) con contenuti come:
    ```ini
    [wsl2]
    memory=4GB  # Esempio: limita a 4GB
    processors=2 # Esempio: limita a 2 processori
    ```
    Dopo aver modificato questo file, riavvia WSL eseguendo `wsl --shutdown` in PowerShell (come amministratore) e poi riaprendo la tua distribuzione WSL.

## Esecuzione

L'applicazione è progettata per essere eseguita con Docker, il che semplifica la gestione delle dipendenze e la configurazione.

### Esecuzione con Docker (Metodo Consigliato per Self-Hosting)

Dopo aver completato i passaggi nella sezione "Setup":

1.  **Apri un terminale** nella cartella (dir) principale del progetto (dove si trova il file `docker-compose.yml`).
2.  **Avvia l'applicazione:**
    ```bash
    docker-compose up --build -d
    ```
    *   `--build`: Questo comando costruirà l'immagine Docker la prima volta o se hai modificato il `Dockerfile` o `requirements.txt`. Puoi ometterlo per avvii successivi se l'immagine non è cambiata.
    *   `-d`: Esegue i container in background (detached mode).
3.  **Attendi l'avvio:** Potrebbe richiedere un minuto o due la prima volta. Puoi controllare i log con:
    ```bash
    docker-compose logs -f app
    ```
    (Premi `Ctrl+C` per uscire dai log).
4.  **Accedi all'applicazione:** Apri il tuo browser web e vai a `http://localhost:5000` (o la porta che hai configurato se hai modificato il `docker-compose.yml` o l'IP/dominio del tuo server).
5.  **Primo Utilizzo - Autenticazione Google:** La prima volta che accedi e provi a usare una funzionalità che richiede l'API YouTube (es. Dashboard > Processa Canale), verrai reindirizzato per autenticarti con Google. Completa il flusso. Il token di accesso verrà salvato in `./data/token.pickle` (o `token.json`) e usato per le sessioni future.

**Per fermare l'applicazione Docker:**
Nella stessa directory del progetto, esegui:
```bash
docker-compose down
```


## Esecuzione con Python (Senza Docker)

Se preferisci eseguire l'applicazione direttamente con Python installato sul tuo sistema, puoi utilizzare i seguenti script o avviare i componenti manualmente.

1.  **Prerequisiti Specifici per Esecuzione Diretta:**
    *   Assicurati di avere **Python 3.8+** installato sul tuo sistema.
    *   Avrai bisogno di aver completato i passaggi 1, 3 (la parte relativa all'ottenimento di `client_secrets.json` da Google Cloud e della tua API Key Gemini), e 4 (configurazione del file `.env`) della sezione "Setup" generale.
    *   **Posizionamento File per Esecuzione Diretta:**
        *   Il file `client_secrets.json` (scaricato da Google Cloud) deve trovarsi nella **directory principale (root)** del progetto.
        *   Il file `.env` (creato da `.env.example`) deve anch'esso trovarsi nella **directory principale (root)** del progetto.
        *   Nel file `.env`, i percorsi come `DATABASE_FILE`, `CHROMA_DB_PATH`, `UPLOAD_FOLDER`, `ARTICLES_FOLDER`, `GOOGLE_CLIENT_SECRETS_FILE`, `GOOGLE_TOKEN_FILE` dovranno essere relativi alla directory principale del progetto (es. `GOOGLE_CLIENT_SECRETS_FILE=client_secrets.json`, `DATABASE_FILE=data/creator_warehouse.db`). Assicurati che la directory `data/` esista nella root del progetto.

2. **Utilizzo degli script:**

Abbiamo preparato degli script per semplificare l'avvio:

*   `run_local.bat` per Windows
*   `run_local.sh` per macOS e Linux

Questi script attivano l'ambiente virtuale Python (necessario per far funzionare l'applicazione con le sue dipendenze) e poi avviano l'applicazione principale.

**Passaggi per avviare l'applicazione con gli script:**

1.  **Apri un terminale o Prompt dei Comandi:** Trova l'applicazione "Terminale" (su macOS/Linux) o "Prompt dei Comandi" (su Windows) sul tuo computer e aprila.
2.  **Naviga nella cartella del progetto:** Usa il comando `cd` seguito dal percorso della cartella dove hai clonato il progetto. Ad esempio:
    ```bash
    cd C:\Users\IlTuoNomeUtente\PercorsoAllaCartellaDelProgetto
    ```
    Sostituisci `C:\Users\IlTuoNomeUtente\PercorsoAllaCartellaDelProgetto` con il percorso effettivo della cartella del progetto sul tuo computer.
3.  **Esegui lo script appropriato:**

    *   **Se usi Windows:**
        Esegui il file `run_local.bat` digitando nel terminale:
        ```bash
        .\run_local.bat
        ```

    *   **Se usi macOS o Linux:**
        Prima di eseguire, potresti dover rendere lo script eseguibile. Apri il terminale nella cartella del progetto e digita:
        ```bash
        chmod +x run_local.sh
        ```
        Poi esegui lo script digitando:
        ```bash
        ./run_local.sh
        ```

Una volta eseguito lo script, l'applicazione dovrebbe avviarsi e potrai accedere all'interfaccia web tramite il tuo browser all'indirizzo `http://localhost:5000`.

---
**Metodo Alternativo (per utenti più tecnici):**

1.  **Attiva l'ambiente virtuale:**
    *   Su Windows: `.\venv\Scripts\activate`
    *   Su macOS/Linux: `source venv/bin/activate`
2.  **Avvia il Backend Flask:** Nello stesso terminale (con l'ambiente virtuale attivo):
    ```bash
    python -m app.main
    ```
---
4.  **Accesso all'Applicazione:**
    L'applicazione backend sarà in esecuzione e accessibile aprendo il tuo browser web e navigando a `http://localhost:5000` (o la porta configurata nel tuo `.env`).

5.  **Primo Utilizzo - Autenticazione Google:**
    Come per l'esecuzione Docker, la prima volta che accedi e provi a usare una funzionalità che richiede l'API YouTube, verrai reindirizzato per autenticarti con Google. Completa il flusso. Il token di accesso (`token.pickle` o `token.json`, come configurato in `.env`) verrà salvato (nel percorso specificato, es. nella root o in `data/`) e usato per le sessioni future.

**Per fermare l'applicazione (quando eseguita direttamente con Python):**
Premi `Ctrl+C` nel terminale dove hai avviato `python -m app.main`.

### Esecuzione Avanzata: Usare l'Immagine Pre-Compilata da GHCR

Se preferisci non costruire l'immagine Docker localmente, puoi utilizzare le immagini stabili che vengono automaticamente costruite e pubblicate su GitHub Container Registry (GHCR) dopo ogni aggiornamento al codice principale.

1.  Assicurati di avere Docker e Docker Compose installati.
2.  Crea una directory per il tuo progetto e naviga al suo interno.
3.  Crea la sottodirectory `data`.
4.  Prepara il tuo file `client_secrets.json` e mettilo in `data/client_secrets.json`.
5.  Crea un file `.env` con le tue configurazioni (vedi sezione "Configura il File d'Ambiente (`.env`)" sopra).
6.  Crea un file `docker-compose.ghcr.yml` (o un nome simile) con il seguente contenuto:

    ```yaml
    version: '3.8'
    services:
      app:
        image: ghcr.io/f041/magazzino-creatore-selfhosted:latest # Usa l'immagine da GHCR
        container_name: magazzino_creatore_app_selfhosted
        ports:
          - "5000:5000" # Modifica la porta host (la prima '5000') se necessario
        volumes:
          - ./data:/app/data # Per i tuoi dati persistenti
        env_file:
          - .env
        restart: unless-stopped
    ```
7.  Avvia l'applicazione:
    ```bash
    docker-compose -f docker-compose.ghcr.yml pull # Scarica l'ultima immagine
    docker-compose -f docker-compose.ghcr.yml up -d
    ```
Questo metodo è utile se vuoi eseguire una versione specifica o se hai difficoltà a buildare l'immagine localmente.

## Utilizzo (Modalità `saas`)

1.  **Registrazione/Login Flask:** Apri `http://localhost:5000`. Registra un nuovo utente o effettua il login.
2.  **Autenticazione Google:** Se necessario, completa il flusso di login Google per autorizzare l'API YouTube.
3.  **Elaborazione Contenuti (Flask UI):** Usa la `/dashboard` per aggiungere contenuti YouTube, Documenti o RSS. Monitora lo stato dei processi asincroni.
4.  **Gestione Contenuti (Flask UI):** Usa le pagine `/my-*` per visualizzare, riprocessare o eliminare contenuti. Usa `/keys/manage` per creare/eliminare chiavi API.
5.  **Interrogazione Contenuti (Chat Flask):**
    *   Vai alla pagina `/chat`.
    *   Poni domande in linguaggio naturale. La ricerca userà i contenuti indicizzati *per il tuo utente*.
6.  **(Opzionale) Incorporare la Chat su un Sito Esterno:**
    *   Vai su `/chat`.
    *   Clicca sul bottone/icona "Incorpora Chat".
    *   Nel modale, segui le istruzioni:
        *   Vai su `/keys/manage` e genera una nuova chiave API (es. "Widget Mio Sito").
        *   Copia la chiave API.
        *   Incolla la chiave API nel campo del modale "Incorpora Chat".
    *   Copia lo snippet `<script>` generato dal modale.
    *   Incolla lo snippet nel codice HTML del tuo sito esterno.
    *   Il widget della chat apparirà sul tuo sito e si autenticherà usando la chiave API fornita.
    *   *(Nota: In modalità `single`, non è richiesta la chiave API nel modale).*

## Note Importanti

*   **Cambio `APP_MODE`:** Passare da `single` a `saas` o viceversa **richiede la re-indicizzazione** di tutti i contenuti per popolare le collezioni ChromaDB corrette per la nuova modalità. Usa i pulsanti "Riprocessa" o implementa uno script/bottone di re-indicizzazione di massa.
*   **Sicurezza:** Non committare `.env`, `client_secrets.json`, `token.pickle`, `*.db`, `data/`. La `FLASK_SECRET_KEY` deve essere robusta. `OAUTHLIB_INSECURE_TRANSPORT=1` solo per sviluppo. Le API Key sono sensibili.

## TODO e Prossimi Passi

**Funzionalità Completate Recentemente:**
*   [x] Gestione Documenti: Upload (PDF, DOCX, TXT), conversione MD, salvataggio, indicizzazione, visualizzazione, eliminazione (incluso ChromaDB).
*   [x] Gestione Articoli RSS: Parsing feed (asincrono con feedback UI), estrazione contenuto, salvataggio file, registrazione DB, indicizzazione, eliminazione tutti articoli.
*   [x] Ricerca Multi-Sorgente: L'API RAG interroga collezioni Video, Documenti e Articoli.
*   [x] Integrazione Chat in Flask (`/chat`) con SSE per feedback stato.
*   [x] Funzionalità Widget Chat Incorporabile (con autenticazione API Key in `saas`).
*   [x] Refactoring Template: Utilizzo di `base.html`.
*   [x] Aggiunta Configurazione `APP_MODE` (`single`/`saas`).
*   [x] Adattamento DB SQLite (colonna `user_id`).
*   [x] Adattamento Inizializzazione ChromaDB per `APP_MODE`.
*   [x] Adattamento API e Liste UI (`/my-*`) per `APP_MODE` e autenticazione Flask-Login.
*   [x] Autenticazione Utente Flask (Registrazione/Login).
*   [x] Gestione API Key (Generazione/Eliminazione base).
*   [x] Autenticazione API `/api/search` condizionale (API Key/Sessione in `saas`, aperta in `single`).
*   [x] Elaborazione Canale YouTube Asincrona (`threading`) con feedback UI.
*   [x] Eliminazione di Massa Video (SQLite + delete collection Chroma) (`saas`).
*   [x] Pulsante "(Ri)Processa" sempre visibile in `/my-videos`.
*   [x] Integrazione APScheduler per controlli periodici (config da `.env`).

**Prossimi Passi Possibili:**

**Completamento Funzionalità SAAS & Gestione Dati:**
*   [ ] **Completare Eliminazione Singola:** (Priorità Alta)
    *   [ ] Implementare `DELETE /api/videos/<id>` (con pulizia chunk ChromaDB).
    *   [ ] Implementare `DELETE /api/articles/<id>` (con pulizia chunk ChromaDB e file `.txt`).
    *   [ ] Aggiungere bottoni "Elimina" corrispondenti in `/my-videos` e `/my-articles`.
*   [ ] **Implementare Re-indicizzazione di Massa (UI):** (Priorità Media/Alta)
    *   [ ] Creare API `POST /api/admin/reindex-all` (o simile) che avvia task background (`threading`).
    *   [ ] Implementare la funzione background che itera su tutti i contenuti dell'utente e li re-indicizza.
    *   [ ] Creare/Estendere endpoint `/api/.../progress` per monitoraggio task.
    *   [ ] Aggiungere bottone e feedback nella UI Flask.

**Miglioramenti RAG/Ricerca:**
*   [ ] **Ottimizzare Recupero:** Sperimentare `n_results`, analizzare chunk, esplorare re-ranking/query expansion.
*   [ ] **Cronologia Conversazione:** Implementare gestione stato conversazione nella chat Flask.
*   [ ] **Renderizzare Markdown nelle Risposte:** Valutare uso libreria JS (Marked.js + DOMPurify) per mostrare formattazione LLM (elenchi, grassetto).

**Stabilità e Qualità:**
*   [ ] **Gestione Errori API:** Standardizzare formati JSON risposte errore.
*   [ ] **Gestione Errori Indicizzazione:** Migliorare diagnostica/gestione errori estrazione testo e embedding.

**Nuove Sorgenti Dati/Funzionalità:**
*   [ ] **Podcast:** Implementare gestione feed/audio/trascrizione.
*   [ ] **Integrazione Bot Telegram:** (Priorità Bassa/Media) Creare script bot separato che usi una chiave API del creator per permettere alla sua community di interrogare i suoi contenuti via Telegram.

**UI/UX:**
*   [ ] **Feedback Processi Background:** Migliorare ulteriormente il feedback per operazioni lunghe (re-indicizzazione).
*   [ ] **Gestione API Key Widget:** Rendere più fluida l'esperienza di ottenimento e inserimento della chiave per l'embed.

**DevOps e Deployment:**
*   [x] **Dockerizzazione:** Creati `Dockerfile` e `docker-compose.yml` per esecuzione self-hosted.
*   [ ] **CI/CD:** Impostare pipeline.
*   [ ] **Valutare DB Esterno:** Considerare PostgreSQL/Supabase per deployment `saas` su larga scala (o per istanze self-hosted che richiedono maggiore robustezza/concorrenza, specialmente se si separa lo scheduler).

**Funzionalità Rimosse/Archiviate:**
*   ~~Interfaccia Chat Streamlit~~ (Integrata in Flask)
*   ~~Streaming Risposte LLM~~
*   ~~Utilizzo Whisper/ASR Esterno~~ (Integrato con gestione documenti)
