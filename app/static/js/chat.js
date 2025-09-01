document.addEventListener('DOMContentLoaded', () => {
    let JWT_TOKEN = null; // Nuova variabile per il nostro token
    
    // Funzione per estrarre il token JWT dal parametro 'token' dell'URL
    function getTokenFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const token = params.get('token');
        if (token) {
            console.log("Chat JS: Token JWT trovato nell'URL.");
            return token;
        }
        console.log("Chat JS: Nessun token JWT trovato nell'URL.");
        return null;
    }
    JWT_TOKEN = getTokenFromUrl(); // Salviamo il token
    
    console.log(`Chat JS loaded. Token JWT presente: ${JWT_TOKEN ? 'Sì' : 'No'}`);

    let chatHistory = [];
    const MAX_HISTORY_MESSAGES_TO_SEND = 6;
    const MAX_LOCAL_HISTORY_ITEMS = 50;

    const chatWindow = document.getElementById('chat-window');
    const userInput = document.getElementById('user-input');
    const sendButton = document.getElementById('send-button');

    if (!chatWindow || !userInput || !sendButton) {
        console.error("Errore critico: Elementi base della chat (finestra, input, bottone) non trovati nel DOM!");
        return;
    }

    function manageChatHistory(newEntry) {
        chatHistory.push(newEntry);
        if (chatHistory.length > MAX_LOCAL_HISTORY_ITEMS) { 
            chatHistory = chatHistory.slice(-MAX_LOCAL_HISTORY_ITEMS); 
            console.log(`Cronologia locale troncata a ${MAX_LOCAL_HISTORY_ITEMS} messaggi.`); 
        }
    }


    function addMessage(content, sender, references = null, isError = false, isLoading = false) {
        const messageDiv = document.createElement('div');
        messageDiv.classList.add('message', `${sender}-message`);
        if (isError) messageDiv.classList.add('error');
        if (isLoading) messageDiv.classList.add('loading');

        if (content && typeof content === 'string' && content.trim() !== '') {
            const paragraph = document.createElement('p');

            // --- MODIFICA QUI ---
            // Se è un messaggio del bot, non di errore e non di loading,
            // ci aspettiamo che 'content' sia GIA' HTML sanificato.
            // Altrimenti (messaggio utente, errore, loading), trattiamo 'content' come testo semplice.
            if (sender === 'bot' && !isLoading && !isError) {
                paragraph.innerHTML = content; // Inseriamo l'HTML direttamente
            } else {
                // Per messaggi utente, errori, o loading, gestiamo come testo semplice
                // e sostituiamo i newline con <br> per la visualizzazione.
                // Non è necessario DOMPurify qui perché non stiamo iniettando HTML dall'utente.
                paragraph.innerHTML = String(content).replace(/\n/g, '<br>');
            }
            // --- FINE MODIFICA ---

            messageDiv.appendChild(paragraph);
        }

        if (sender === 'bot' && references && Array.isArray(references) && references.length > 0) {
            const refsContainer = document.createElement('div');
            refsContainer.classList.add('references-container');
            const toggleButton = document.createElement('button');
            toggleButton.classList.add('references-toggle');
            toggleButton.textContent = `Riferimenti (${references.length}) ▼`;
            const refsList = document.createElement('ul');
            refsList.classList.add('references-list');
            refsList.style.display = 'none';
            references.forEach(ref => {
                const refItem = document.createElement('li');
                refItem.classList.add('reference-item');
                const metadata = ref.metadata || {};
                const distance = ref.distance !== undefined ? ` (Dist: ${ref.distance.toFixed(4)})` : '';
                let title = 'N/D'; let link = '#'; let sourcePrefix = ''; let details = '';
                if (metadata.source_type === 'video') { sourcePrefix = 'Video:'; title = metadata.video_title || 'N/D'; if (metadata.video_id) link = `https://www.youtube.com/watch?v=${metadata.video_id}`; details = `Canale: ${metadata.channel_id||'N/D'} | Pubbl: ${metadata.published_at?new Date(metadata.published_at).toLocaleDateString():'N/D'} | Tipo: ${metadata.caption_type||'N/D'}`; }
                else if (metadata.source_type === 'document') { sourcePrefix = 'Doc:'; title = metadata.original_filename || 'N/D'; }
                else if (metadata.source_type === 'article') { sourcePrefix = 'Articolo:'; title = metadata.article_title || metadata.title || 'N/D'; link = metadata.article_url || metadata.url || '#'; }
                else { sourcePrefix = 'Fonte ??:'; title = metadata.doc_id || metadata.article_id || metadata.video_id || 'ID N/D'; }
                let refHTML = `<p><strong>${sourcePrefix}</strong> `;
                if (link !== '#') refHTML += `<a href="${link}" target="_blank" rel="noopener noreferrer">${title}</a>`; else refHTML += title;
                refHTML += `${distance}</p>`; if(details) refHTML += `<p><small>${details}</small></p>`;

                // Anche per l'anteprima del riferimento, se contiene markdown, potremmo volerlo renderizzare.
                // Ma per semplicità e sicurezza, per ora la lasciamo come testo semplice.
                // Se volessi renderizzare anche questo, dovresti fare:
                // const previewText = ref.text ? DOMPurify.sanitize(marked.parse(ref.text.substring(0,150) + ...)) : ...
                // Attenzione: marked.parse(null) o marked.parse(undefined) darebbero errore.
                const textPreview = ref.text ? (String(ref.text).substring(0, 150) + (ref.text.length > 150 ? '...' : '')) : 'N/A.';
                refHTML += `<p class="reference-preview">${textPreview.replace(/\n/g, '<br>')}</p>`; // Sostituisci newline per l'anteprima

                refItem.innerHTML = refHTML;
                refsList.appendChild(refItem);
            });
            toggleButton.addEventListener('click', function(){ const isHidden=refsList.style.display==='none'; refsList.style.display=isHidden?'block':'none'; toggleButton.textContent=`Riferimenti (${references.length}) ${isHidden?'▲':'▼'}`; });
            refsContainer.appendChild(toggleButton); refsContainer.appendChild(refsList); messageDiv.appendChild(refsContainer);
        }

        if (messageDiv.hasChildNodes() || isLoading) {
             chatWindow.appendChild(messageDiv);
             chatWindow.scrollTop = chatWindow.scrollHeight;
             return messageDiv;
        }
        return null;
    }

    function enableUI(enable = true) {
        userInput.disabled = !enable;
        sendButton.disabled = !enable;
        if (enable) userInput.focus();
    }

    async function handleSendMessage() {
        const query = userInput.value.trim();
        if (!query || sendButton.disabled) return;

        addMessage(query, 'user');
        userInput.value = '';
        userInput.style.height = 'auto';
        enableUI(false);
        let currentStatusMessageElement = addMessage("Elaborazione in corso...", 'bot', null, false, true);

        manageChatHistory({ role: "user", content: query });

        const payload = {
            query: query,
        };

        console.log("Controllo se inviare cronologia. chatHistory.length:", chatHistory.length);
        if (chatHistory.length > 1) {
            const historyForPrompt = chatHistory.slice(0, -1);
            const historyToSend = historyForPrompt.slice(-MAX_HISTORY_MESSAGES_TO_SEND);
            if (historyToSend.length > 0) {
                payload.history = historyToSend;
            }
        }

        try {
            const fetchHeaders = {
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream'
            };

            // NUOVA LOGICA: Aggiungi l'header di autorizzazione JWT se abbiamo un token
            if (JWT_TOKEN) {
                // Questo è lo standard per inviare i token JWT
                fetchHeaders['Authorization'] = `Bearer ${JWT_TOKEN}`;
            }

            const response = await fetch('/api/search/', {
                method: 'POST',
                headers: fetchHeaders,
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                let errorMsg = `Errore HTTP: ${response.status}`;
                try {
                    const errData = await response.json();
                    errorMsg = errData.message || errData.error || errorMsg;
                } catch (e) {/* non era JSON */}
                throw new Error(errorMsg);
            }

            const contentType = response.headers.get('content-type');
            if (!contentType || !contentType.includes('text/event-stream')) {
                throw new Error('La risposta del server non è di tipo text/event-stream.');
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';
            let finalResultReceived = false;

            function processStreamChunk() {
                reader.read().then(({ value, done }) => {
                    if (done) {
                        console.log("Stream SSE completato.");
                        if (currentStatusMessageElement && chatWindow.contains(currentStatusMessageElement)) {
                            currentStatusMessageElement.remove();
                        }
                        if (!finalResultReceived) {
                           addMessage("La risposta dal server sembra incompleta.", 'bot', null, true);
                        }
                        enableUI(true);
                        return;
                    }

                    buffer += decoder.decode(value, { stream: true });
                    const events = buffer.split('\n\n');
                    buffer = events.pop();

                    events.forEach(eventString => {
                        if (!eventString.trim()) return;

                        let eventType = 'message';
                        let eventDataString = '';

                        eventString.split('\n').forEach(line => {
                            if (line.startsWith('event:')) {
                                eventType = line.substring(6).trim();
                            } else if (line.startsWith('data:')) {
                                eventDataString += line.substring(5);
                            }
                        });

                        try {
                            if (!eventDataString.trim()) return;
                            const eventData = JSON.parse(eventDataString);

                            if (eventType === 'status') {
                                if (currentStatusMessageElement && eventData.message) {
                                    currentStatusMessageElement.querySelector('p').innerHTML = eventData.message.replace(/\n/g, '<br>');
                                }
                            } else if (eventType === 'result' || eventType === 'error_final') {
                                finalResultReceived = true;
                                if (currentStatusMessageElement && chatWindow.contains(currentStatusMessageElement)) {
                                    currentStatusMessageElement.remove();
                                }
                                if (eventData.success && eventData.answer) {
                                    if (eventData.answer.startsWith("BLOCKED:")) {
                                        const reason = eventData.answer.split(":", 2)[1] || "Ragione sconosciuta";
                                        addMessage(`⚠️ Risposta bloccata (${reason}). Riprova.`, 'bot', null, true);
                                    } else {
        
                                        // 1. Converti Markdown in HTML usando Marked.js
                                        const rawHtml = marked.parse(eventData.answer);
                                        // 2. Sanifica l'HTML usando DOMPurify
                                        const sanitizedHtml = DOMPurify.sanitize(rawHtml);
                                        // 3. Passa l'HTML sanificato ad addMessage
                                        addMessage(sanitizedHtml, 'bot', eventData.retrieved_results);
   
                                        manageChatHistory({ role: "assistant", content: eventData.answer });                                    }
                                } else {
                                    addMessage(eventData.message || `Errore: ${eventData.error_code || 'Sconosciuto'}`, 'bot', null, true);
                                }
                            }
                        } catch (e) {
                            console.error("Errore parsing JSON da evento SSE:", e, "Stringa evento:", JSON.stringify(eventDataString));
                        }
                    });
                    processStreamChunk();
                }).catch(streamError => {
                    console.error('Errore lettura stream SSE:', streamError);
                    if (currentStatusMessageElement && chatWindow.contains(currentStatusMessageElement)) {
                        currentStatusMessageElement.remove();
                    }
                    addMessage(`Errore di comunicazione con il server: ${streamError.message}`, 'bot', null, true);
                    enableUI(true);
                });
            }
            processStreamChunk();

        } catch (error) {
            console.error('Errore invio query o gestione risposta iniziale:', error);
            if (currentStatusMessageElement && chatWindow.contains(currentStatusMessageElement)) {
                currentStatusMessageElement.remove();
            }
            addMessage(`Si è verificato un errore: ${error.message}`, 'bot', null, true);
            enableUI(true);
        }
    }

    userInput.addEventListener('input', () => {
    userInput.style.height = 'auto'; // Resetta l'altezza per calcolare la nuova
    userInput.style.height = (userInput.scrollHeight) + 'px'; // Imposta l'altezza in base al contenuto
});

    sendButton.addEventListener('click', handleSendMessage);
    userInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            handleSendMessage();
        }
    });

    

    userInput.focus();
    console.log("Chat JS inizializzato e pronto.");
});
