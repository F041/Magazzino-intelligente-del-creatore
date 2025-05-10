document.addEventListener('DOMContentLoaded', () => {
    let WIDGET_API_KEY = null;
    function getApiKeyFromUrl() {
        const params = new URLSearchParams(window.location.search); const key = params.get('apiKey');
        if(key){console.log("Chat JS (Widget): API Key trovata."); return key;} else {console.log("Chat JS (Widget): No API Key in URL."); return null;}
    }
    WIDGET_API_KEY = getApiKeyFromUrl();
    console.log(`Chat JS loaded (SSE - Final Fix Attempt). API Key: ${WIDGET_API_KEY || "None"}`);

    // Se la Priorità è la Ricchezza/Leggibilità della Risposta: Implementa la renderizzazione Markdown. 
    // Marked.js è spesso una buona scelta per iniziare perché è relativamente semplice da usare. 
    // È FONDAMENTALE includere la sanificazione dell'output HTML.
    function stripMarkdown(text) {
        if (!text) return '';
        let cleaned = String(text);
        // Rimuove **testo** -> testo
        cleaned = cleaned.replace(/\*\*(.*?)\*\*/gs, '$1');
        // Rimuove __testo__ -> testo
        cleaned = cleaned.replace(/__(.*?)__/gs, '$1');
        // Rimuove *testo* -> testo (semplificato, potrebbe rimuovere asterischi singoli)
        // Se vuoi essere più preciso, usa la regex negativa che avevamo prima
        cleaned = cleaned.replace(/\*(.*?)\*/gs, '$1');
         // Rimuove _testo_ -> testo (semplificato)
        cleaned = cleaned.replace(/_(.*?)_/gs, '$1');
        // Rimuove `codice` -> codice
        cleaned = cleaned.replace(/`(.*?)`/g, '$1');
        // Rimuove ```blocco``` -> blocco
        cleaned = cleaned.replace(/```([\s\S]*?)```/g, '$1');
        // Potresti aggiungere qui la rimozione di # per gli header se necessario
        // cleaned = cleaned.replace(/^#+\s+/gm, ''); // Rimuove # all'inizio di una riga
        return cleaned.trim(); // Rimuove spazi extra alla fine
    }

    const chatWindow = document.getElementById('chat-window');
    const userInput = document.getElementById('user-input');
    const sendButton = document.getElementById('send-button');
    if (!chatWindow || !userInput || !sendButton) { console.error("Errore: Elementi chat non trovati."); return; }
    console.log("Elementi chat trovati:", { chatWindow, userInput, sendButton });

    // --- Funzione addMessage (COMPLETA) ---
    function addMessage(text, sender, references = null, table_data = null, isError = false) {
        console.log(`-> addMessage START: sender='${sender}', isError=${isError}, text=`, text ? text.substring(0,50)+'...' : 'None');
        const messageDiv = document.createElement('div');

        // --- Aggiunta Classi Sequenziale (Corretta) ---
        console.log(`-> addMessage: Preparing to add classes for sender '${sender}'`);
        try {
            messageDiv.classList.add('message');
            console.log("-> addMessage: Added class 'message'");
            const senderClass = `${sender}-message`;
            if (/\s/.test(senderClass)) { console.error(`-> addMessage ERROR: Whitespace detected in senderClass '${senderClass}'!`); }
            else { messageDiv.classList.add(senderClass); console.log(`-> addMessage: Added class '${senderClass}'`); }
            if (isError && sender === 'bot') { messageDiv.classList.add('error'); console.log("-> addMessage: Added class 'error'"); }
        } catch (e) { console.error(`-> addMessage: DOMException during classList.add! Sender was: '${sender}'.`, e); }
        // --- Fine Aggiunta Classi ---

        // Aggiungi testo paragrafo
        if (text && typeof text === 'string' && text.trim() !== '') {
            const paragraph = document.createElement('p');
        
            // Solo se il messaggio è dal bot, applichiamo la pulizia
            const cleanedText = (sender === 'bot') ? stripMarkdown(text) : text;
            // -----------------------------------
        
            // Usa il testo pulito per impostare innerHTML
            paragraph.innerHTML = cleanedText.replace(/\n/g, '<br>');
            messageDiv.appendChild(paragraph);
        }

        // Aggiungi riferimenti (Logica Completa Ripristinata)
        if (sender === 'bot' && references && Array.isArray(references) && references.length > 0) {
            const refsContainer = document.createElement('div');
            refsContainer.classList.add('references-container');
            const toggleButton = document.createElement('button');
            toggleButton.classList.add('references-toggle');
            toggleButton.textContent = `Riferimenti (${references.length}) ▼`;
            const refsList = document.createElement('div');
            refsList.classList.add('references-list');
            refsList.style.display = 'none';
            references.forEach(ref => {
                const refItem = document.createElement('div');
                refItem.classList.add('reference-item');
                const metadata = ref.metadata || {};
                const distance = ref.distance !== undefined ? ` (Dist: ${ref.distance.toFixed(4)})` : '';
                let title = 'N/D'; let link = '#'; let sourcePrefix = ''; let details = '';
                // Formattazione per tipo sorgente (Ripristinata)
                if (metadata.source_type === 'video') { sourcePrefix = 'Video:'; title = metadata.video_title || 'N/D'; if (metadata.video_id) link = `https://www.youtube.com/watch?v=${metadata.video_id}`; details = `Canale: ${metadata.channel_id||'N/D'} | Pubbl: ${metadata.published_at?new Date(metadata.published_at).toLocaleDateString():'N/D'} | Tipo: ${metadata.caption_type||'N/D'}`; }
                else if (metadata.source_type === 'document') { sourcePrefix = 'Doc:'; title = metadata.original_filename || 'N/D'; }
                else if (metadata.source_type === 'article') { sourcePrefix = 'Articolo:'; title = metadata.article_title || metadata.title || 'N/D'; link = metadata.article_url || metadata.url || '#'; }
                else { sourcePrefix = 'Fonte ??:'; title = metadata.doc_id || metadata.article_id || metadata.video_id || 'ID N/D'; }
                let refHTML = `<p><strong>${sourcePrefix}</strong> `;
                if (link !== '#') refHTML += `<a href="${link}" target="_blank">${title}</a>`; else refHTML += title;
                refHTML += `${distance}</p>`; if(details) refHTML += `<p><small>${details}</small></p>`;
                const textPreview = ref.text ? (ref.text.substring(0, 150) + (ref.text.length > 150 ? '...' : '')) : 'N/A.';
                refHTML += `<p class="reference-preview">${textPreview}</p>`;
                refItem.innerHTML = refHTML;
                refsList.appendChild(refItem);
            });
            toggleButton.addEventListener('click', function(){ const isHidden=refsList.style.display==='none'; refsList.style.display=isHidden?'block':'none'; toggleButton.textContent=`Riferimenti (${references.length}) ${isHidden?'▲':'▼'}`; });
            refsContainer.appendChild(toggleButton); refsContainer.appendChild(refsList); messageDiv.appendChild(refsContainer);
        } // Fine logica riferimenti

        // Aggiungi al DOM
        if (messageDiv.hasChildNodes()) {
             chatWindow.appendChild(messageDiv);
             chatWindow.scrollTop = chatWindow.scrollHeight;
             console.log("-> addMessage END: Messaggio aggiunto.");
             return messageDiv;
        } else {
            console.warn("-> addMessage END: Messaggio vuoto non aggiunto.");
            return null;
        }
    } // --- FINE Funzione addMessage ---

    // --- Funzione enableUI (COMPLETA) ---
    function enableUI() {
        console.log("-> enableUI: Riabilitazione UI.");
        if (userInput) userInput.disabled = false;
        if (sendButton) sendButton.disabled = false;
        if (userInput) userInput.focus();
    } // --- FINE Funzione enableUI ---


    // --- Funzione sendQuerySSE (COMPLETA) ---
    function sendQuerySSE() {
        console.log("-> sendQuerySSE: Avviata.");
        const query = userInput.value.trim(); if (!query) return;
        addMessage(query, 'user'); // Aggiunge domanda utente
        userInput.value = ''; userInput.disabled = true; sendButton.disabled = true;
        let statusMessageElement = null; // Resetta per ogni nuova query

        try {
            // Crea messaggio di stato iniziale USANDO 'bot' come sender
            statusMessageElement = addMessage("Avvio richiesta...", 'bot', null, null, false);
            if (statusMessageElement) {
                // AGGIUNGI la classe 'loading' SEPARATAMENTE
                statusMessageElement.classList.add('loading');
                console.log("-> sendQuerySSE: Messaggio stato iniziale ('bot' + 'loading') aggiunto.");
            } else { console.error("-> sendQuerySSE: Fallimento creazione statusMessageElement!"); enableUI(); return; }

            const fetchHeaders = { 'Content-Type': 'application/json', 'Accept': 'text/event-stream', ...(WIDGET_API_KEY && {'X-API-Key': WIDGET_API_KEY}) };
            console.log("-> sendQuerySSE: Avvio fetch a /api/search/ con headers:", fetchHeaders);

            fetch('/api/search/', { method: 'POST', headers: fetchHeaders, body: JSON.stringify({ query: query }) })
            .then(response => {
                console.log(`-> Fetch response: Status=${response.status}, OK=${response.ok}`);
                if (!response.ok) { /* Gestione errore HTTP iniziale */ throw new Error(`Errore HTTP ${response.status}`); } // Semplificato per brevità
                const contentType = response.headers.get('content-type');
                if (!contentType || !contentType.includes('text/event-stream')) { throw new Error('Risposta non è event-stream.'); }

                // Rimuovi stato INIZIALE ("Avvio richiesta...")
                if (statusMessageElement && chatWindow.contains(statusMessageElement)) {
                    statusMessageElement.remove();
                    statusMessageElement = null; // Resetta la variabile
                    console.log("-> Stato iniziale rimosso.");
                }
                // Crea NUOVO elemento per gli stati SSE
                statusMessageElement = addMessage("...", 'bot', null, null, false); // Crea con sender 'bot'
                if (statusMessageElement) {
                    statusMessageElement.classList.add('loading'); // Aggiungi 'loading' per stile
                    console.log("-> Creato nuovo statusMessageElement per stati SSE.");
                } else {
                    console.error("-> Impossibile creare statusMessageElement per SSE!");
                    // Errore grave, ma proviamo a continuare senza aggiornare stati? O usciamo?
                    // Usciamo per sicurezza
                    enableUI(); return;
                }


                const reader = response.body.getReader(); const decoder = new TextDecoder('utf-8'); let buffer = ''; let finalResultReceived = false;

                             // --- Funzione processStream (con gestione stato corretta) ---
                function processStream() {
                    reader.read().then(({ value, done }) => {
                        if (done) {
                            console.log("-> processStream: DONE=true.");
                            if (!finalResultReceived && statusMessageElement) { console.warn("-> Stream finito senza evento finale."); statusMessageElement.remove(); addMessage("Risposta incompleta.",'bot',null,null,true); }
                            else if (statusMessageElement && chatWindow.contains(statusMessageElement)) { console.log("-> Rimuovo stato residuo (done)."); statusMessageElement.remove(); }
                            enableUI(); return;
                        }
                        buffer += decoder.decode(value, { stream: true }); const events = buffer.split('\n\n'); buffer = events.pop();
                        events.forEach(eventString => {
                            if (!eventString.trim()) return; let eventType = 'status'; let eventDataString = '';
                            eventString.split('\n').forEach(line => { if(line.startsWith('event:')) eventType = line.substring(7).trim(); else if(line.startsWith('data:')) eventDataString += line.substring(5); });
                            try {
                                if (!eventDataString.trim()) { console.warn("--> Evento SSE vuoto skippato."); return; }
                                const eventData = JSON.parse(eventDataString);
                                if (eventType === 'status') {
                                    if (statusMessageElement && eventData.message) { // Aggiorna il *nuovo* elemento stato
                                        console.log(`--> Aggiorno stato: "${eventData.message}"`);
                                        statusMessageElement.querySelector('p').innerHTML = eventData.message.replace(/\n/g, '<br>');
                                    } else { console.warn("--> Ricevuto status ma statusMessageElement non valido?"); }
                                } else if (eventType === 'result' || eventType === 'error_final') {
                                    console.log("--> Ricevuto evento finale:", eventType);
                                    finalResultReceived = true;
                                    if (statusMessageElement) { statusMessageElement.remove(); statusMessageElement = null; console.log("--> Messaggio stato SSE rimosso."); }
                                    if (eventData.success) { addMessage(eventData.answer, 'bot', eventData.retrieved_results || [], null, false); }
                                    else { addMessage(`Errore: ${eventData.message || "Errore sconosciuto."}`, 'bot', eventData.retrieved_results || [], null, true); }
                                }
                            } catch (e) { console.error("--> Errore parsing JSON evento SSE:", e, "Stringa:", JSON.stringify(eventDataString)); }
                        });
                        processStream(); // Continua
                    }).catch(streamError => { console.error('-> Errore lettura stream:', streamError); if (statusMessageElement) statusMessageElement.remove(); addMessage(`Errore comunicazione: ${streamError.message}`, 'bot', null, null, true); enableUI(); });
                } // --- FINE processStream ---
                processStream(); // Avvia lettura
            })
            .catch(networkOrHttpError => { /* ... gestione errore fetch iniziale ... */
                 console.error('-> Errore fetch/http iniziale:', networkOrHttpError);
                 if (statusMessageElement) statusMessageElement.remove(); // Rimuovi "Avvio richiesta..."
                 addMessage(`Errore connessione/richiesta: ${networkOrHttpError.message}`, 'bot', null, null, true);
                 enableUI();
             });
        } catch (outerError) { /* ... gestione errore JS esterno ... */
             console.error("-> Errore JS prima della fetch:", outerError);
             if (statusMessageElement) statusMessageElement.remove();
             addMessage(`Errore interno script: ${outerError.message}`, 'bot', null, null, true);
             enableUI();
        }
    } // --- FINE sendQuerySSE ---

    // --- Event Listener ---
    sendButton.addEventListener('click', sendQuerySSE);
    userInput.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendQuerySSE(); } });
    userInput.focus();
    console.log("Chat JS Inizializzato (SSE - Final Fix Attempt).");

}); // Fine DOMContentLoaded