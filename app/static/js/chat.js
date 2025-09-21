document.addEventListener('DOMContentLoaded', () => {
    let JWT_TOKEN = null; 
    
    // Funzione per estrarre il token JWT dal parametro 'token' dell'URL
    function getTokenFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const token = params.get('token');
        if (token) return token;
        return null;
    }
    JWT_TOKEN = getTokenFromUrl(); 
    
    console.log(`Chat JS loaded. Token JWT presente: ${JWT_TOKEN ? 'Sì' : 'No'}`);

    let chatHistory = [];
    let currentMode = 'chat'; // 'chat' o 'idea'
    const MAX_HISTORY_MESSAGES_TO_SEND = 6;
    const MAX_LOCAL_HISTORY_ITEMS = 50;

    const chatWindow = document.getElementById('messages-list'); 
    const userInput = document.getElementById('user-input');
    const sendButton = document.getElementById('send-button');
    const ideaGeneratorBtn = document.getElementById('idea-generator-btn'); // Il nostro nuovo pulsante lampadina
    const exitIdeaModeBtn = document.getElementById('exit-idea-mode-btn'); // Il pulsante 'X' per uscire
    const promptStartersContainer = document.getElementById('prompt-starters');

    if (!chatWindow || !userInput || !sendButton || !ideaGeneratorBtn || !exitIdeaModeBtn) {
        console.error("Elementi UI della chat mancanti! Impossibile inizializzare.");
        return;
    }

    // --- LOGICA DI GESTIONE DINAMICA DELL'INTERFACCIA (REVISIONATA E CORRETTA) ---
    
    // Questa funzione è il cuore dello stato UI: decide cosa mostrare e cosa abilitare
function updateUIState() {
    const hasText = userInput.value.trim().length > 0;
    const sendBtnIcon = document.getElementById('send-button-icon');
    const sendBtnText = document.getElementById('send-button-text');

    // Reset visibilità di tutti i pulsanti icona
    ideaGeneratorBtn.style.display = 'none';
    exitIdeaModeBtn.style.display = 'none';
    
    // Impostazioni di base per la textarea (torna editabile per default)
    userInput.readOnly = false;
    userInput.placeholder = 'Chiedi qualcosa...';

    if (currentMode === 'idea') {
        // *** MODALITÀ IDEE ATTIVA ***
        sendButton.style.display = 'inline-flex';
        sendButton.disabled = false; // Il pulsante "Altre" è sempre attivo in questa modalità
        sendButton.classList.add('idea-mode');
        sendBtnIcon.className = 'fas fa-sync-alt'; // Icona di refresh
        sendBtnText.textContent = 'Altre';

        exitIdeaModeBtn.style.display = 'inline-flex'; // La "X" è visibile
        exitIdeaModeBtn.disabled = false;

        // Rendi la textarea non editabile e chiarisci il placeholder
        userInput.readOnly = true;
        userInput.placeholder = 'Modalità idee — premi "Altre" per nuovi suggerimenti o premi la x a sinistra per uscire';
        // Rimuovi eventuale testo selezionabile per chiarezza (ma non svuotare)
        // userInput.value = ''; // (non forzare lo svuotamento qui per non perdere stato)
    } else {
        // *** MODALITÀ CHAT NORMALE ***
        sendButton.classList.remove('idea-mode'); // Assicurati che non abbia lo stile "idea"
        sendBtnIcon.className = 'fas fa-paper-plane'; // Icona normale
        sendBtnText.textContent = 'Invia';

        exitIdeaModeBtn.style.display = 'none'; // La "X" è nascosta
        exitIdeaModeBtn.disabled = true;

        // In chat normale la textarea è editabile
        userInput.readOnly = false;
        userInput.placeholder = 'Chiedi qualcosa...';

        if (hasText) {
            // C'è testo nell'input: mostra "Invia", nascondi "Lampadina"
            sendButton.style.display = 'inline-flex';
            sendButton.disabled = false;
            ideaGeneratorBtn.style.display = 'none';
            ideaGeneratorBtn.disabled = true;
        } else {
            // Non c'è testo: mostra "Lampadina", nascondi "Invia"
            sendButton.style.display = 'none'; // "Invia" è nascosto se non c'è testo
            sendButton.disabled = true; // "Invia" è disabilitato se nascosto o vuoto
            ideaGeneratorBtn.style.display = 'inline-flex';
            ideaGeneratorBtn.disabled = false; // La lampadina è abilitata se visibile
        }
    }
    // Assicurati che i pulsanti icona disabilitati siano effettivamente disabilitati
    if (ideaGeneratorBtn.style.display === 'none') ideaGeneratorBtn.disabled = true;
    if (exitIdeaModeBtn.style.display === 'none') exitIdeaModeBtn.disabled = true;
}
    
    // Questa funzione abilita/disabilita l'intera interfaccia utente durante un'operazione API
    // È essenziale che chiami updateUIState() quando riabilita.
    function enableFullUI(enable = true) {
        userInput.disabled = !enable;
        if (enable) {
            updateUIState(); // REIMPORTANTE: aggiorna lo stato dei pulsanti alla fine dell'operazione
            userInput.focus();
        } else {
            // Quando si disabilita l'UI, disabilita esplicitamente TUTTI i pulsanti
            sendButton.disabled = true;
            ideaGeneratorBtn.disabled = true;
            exitIdeaModeBtn.disabled = true;
        }
    }

    // --- FUNZIONI HELPER PER I MESSAGGI ---

    function setupPromptStarters() {
        if (!promptStartersContainer) return;

        promptStartersContainer.querySelectorAll('.prompt-starter-btn').forEach((button) => {
            button.addEventListener('click', () => {
                const promptText = button.textContent.trim();
                userInput.value = promptText;
                userInput.dispatchEvent(new Event('input', { bubbles: true })); 
                userInput.focus();
                
                setTimeout(() => {
                    // In modalità chat e con testo, manda il messaggio
                    if (currentMode === 'chat' && userInput.value.trim().length > 0 && !sendButton.disabled) {
                        handleSendMessage();
                    } else if (currentMode === 'idea' && !sendButton.disabled) {
                        // Se in modalità idea e il pulsante "Altre" è attivo, cliccalo
                        handleGenerateIdeas();
                    }
                }, 50);
            });
        });
    }

    function addMessage(content, sender, references = null, isError = false, isLoading = false, performanceMetrics = null) {
        const messageDiv = document.createElement('div');
        messageDiv.classList.add('message', `${sender}-message`);
        if (isError) messageDiv.classList.add('error');
        if (isLoading) messageDiv.classList.add('loading');

        if (content && typeof content === 'string' && content.trim() !== '') {
            const paragraph = document.createElement('p');
            if (sender === 'bot' && !isLoading && !isError) {
                const rawHtml = marked.parse(content);
                paragraph.innerHTML = DOMPurify.sanitize(rawHtml);
            } else {
                paragraph.innerHTML = String(content).replace(/\n/g, '<br>');
            }
            messageDiv.appendChild(paragraph);
        }

        // --- BLOCCO RIFERIMENTI (INVARIATO) ---
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
                if (metadata.source_type === 'video') {
                    sourcePrefix = 'Video:';
                    title = metadata.video_title || 'N/D';
                    if (metadata.video_id) link = `https://www.youtube.com/watch?v=${metadata.video_id}`;
                    details = `Canale: ${metadata.channel_id||'N/D'} | Pubbl: ${metadata.published_at?new Date(metadata.published_at).toLocaleDateString():'N/D'} | Tipo: ${metadata.caption_type||'N/D'}`;
                }
                else if (metadata.source_type === 'document') {
                    sourcePrefix = 'Doc:';
                    title = metadata.original_filename || 'N/D';
                }
                else if (metadata.source_type === 'article') {
                    sourcePrefix = 'Articolo:';
                    title = metadata.article_title || 'N/D';
                    link = metadata.article_url || '#';
                }
                else if (metadata.source_type === 'page') {
                    sourcePrefix = 'Pagina:';
                    title = metadata.page_title || 'N/D';
                    link = metadata.page_url || '#';
                }
                else {
                    sourcePrefix = 'Fonte ??:';
                    title = metadata.doc_id || metadata.article_id || metadata.video_id || metadata.page_id || 'ID N/D';
                }
                let refHTML = `<p><strong>${sourcePrefix}</strong> `;
                if (link !== '#') refHTML += `<a href="${link}" target="_blank" rel="noopener noreferrer">${title}</a>`; else refHTML += title;
                refHTML += `${distance}</p>`; if(details) refHTML += `<p><small>${details}</small></p>`;

                const textPreview = ref.text ? (String(ref.text).substring(0, 150) + (ref.text.length > 150 ? '...' : '')) : 'N/A.';
                refHTML += `<p class="reference-preview">${textPreview.replace(/\n/g, '<br>')}</p>`; 

                refItem.innerHTML = refHTML;
                refsList.appendChild(refItem);
            });
            toggleButton.addEventListener('click', function(){ const isHidden=refsList.style.display==='none'; refsList.style.display=isHidden?'block':'none'; toggleButton.textContent=`Riferimenti (${references.length}) ${isHidden?'▲':'▼'}`; });
            refsContainer.appendChild(toggleButton); refsContainer.appendChild(refsList); messageDiv.appendChild(refsContainer);
        }

        // --- BLOCCO METRICHE DI PERFORMANCE (INVARIATO) ---
        if (sender === 'bot' && performanceMetrics && !isError && !isLoading) {
            const metricsContainer = document.createElement('div');
            metricsContainer.classList.add('metrics-container');
            metricsContainer.style.display = 'none';

            const metricsToggle = document.createElement('button');
            metricsToggle.classList.add('metrics-toggle');
            metricsToggle.textContent = 'Dettagli Performance ▼';
            metricsToggle.addEventListener('click', function() {
                const isHidden = metricsContainer.style.display === 'none';
                metricsContainer.style.display = isHidden ? 'block' : 'none';
                metricsToggle.textContent = `Dettagli Performance ${isHidden ? '▲' : '▼'}`;
            });

            function createMetricRow(label, value, totalDuration = null, barColor = 'var(--color-primary)') {
                const p = document.createElement('p');
                let displayValue = value !== undefined && value !== null ? value : 'N/D';

                if (typeof displayValue === 'number') {
                    p.innerHTML = `<strong>${label}:</strong> ${displayValue.toFixed(0)} ms`;
                    if (totalDuration && totalDuration > 0) {
                        const percentage = (displayValue / totalDuration) * 100;
                        const barContainer = document.createElement('div');
                        barContainer.classList.add('duration-bar-container');
                        const bar = document.createElement('div');
                        bar.classList.add('duration-bar');
                        bar.style.width = `${Math.min(percentage, 100)}%`;
                        bar.style.backgroundColor = barColor;
                        barContainer.appendChild(bar);
                        p.appendChild(barContainer);
                    }
                } else {
                    p.innerHTML = `<strong>${label}:</strong> ${displayValue}`;
                }
                return p;
            }

            const totalDuration = performanceMetrics.total_duration_ms;

            metricsContainer.appendChild(createMetricRow('Totale', totalDuration, totalDuration, 'var(--color-secondary)'));
            metricsContainer.appendChild(createMetricRow('Embedding', performanceMetrics.embedding_duration_ms, totalDuration, '#4cc9f0'));
            metricsContainer.appendChild(createMetricRow('Ricerca Vettoriale', performanceMetrics.retrieval_duration_ms, totalDuration, '#4895ef'));
            
            if (performanceMetrics.reranking_duration_ms !== undefined && performanceMetrics.reranking_duration_ms > 0) {
                metricsContainer.appendChild(createMetricRow('Re-ranking Cohere', performanceMetrics.reranking_duration_ms, totalDuration, '#f72585'));
            }
            
            metricsContainer.appendChild(createMetricRow('Generazione LLM', performanceMetrics.llm_generation_duration_ms, totalDuration, '#7209b7'));
            metricsContainer.appendChild(createMetricRow('Modello LLM usato', performanceMetrics.llm_model_used));
            metricsContainer.appendChild(createMetricRow('Chunk recuperati', performanceMetrics.retrieved_chunks_count));

            messageDiv.appendChild(metricsToggle);
            messageDiv.appendChild(metricsContainer);
        }

        const chatContainerElement = document.getElementById('messages-list');
        if ((messageDiv.hasChildNodes() || isLoading) && chatContainerElement) {
            chatContainerElement.appendChild(messageDiv);
            chatContainerElement.scrollTop = chatContainerElement.scrollHeight;
            return messageDiv;
        }
        return null;
    }

    // --- FUNZIONE PER INVIARE IL MESSAGGIO TESTO ALL'API ---
    async function handleSendMessage() {
        const query = userInput.value.trim();
        
        // Se siamo in modalità 'idea' o se l'input è vuoto, e clicchiamo Invio,
        // chiamiamo handleGenerateIdeas()
        if (currentMode === 'idea' || !query) { 
            handleGenerateIdeas();
            return; 
        }

        // Se siamo qui, è modalità 'chat' normale E c'è testo
        if (promptStartersContainer) {
            promptStartersContainer.style.display = 'none';
        }

        addMessage(query, 'user');
        userInput.value = '';
        userInput.style.height = 'auto';
        enableFullUI(false); 
        let currentStatusMessageElement = addMessage("Elaborazione in corso...", 'bot', null, false, true);

        const payload = { query: query };
        if (chatHistory.length > 0) {
            const historyForPrompt = chatHistory.slice(-MAX_HISTORY_MESSAGES_TO_SEND);
            payload.history = historyForPrompt;
        }

        try {
            const fetchHeaders = {
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream'
            };
            if (JWT_TOKEN) {
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
            let receivedPerformanceMetrics = null; 

            function processStreamChunk() {
                reader.read().then(({ value, done }) => {
                    if (done) {
                        console.log("Stream SSE completato.");
                        if (currentStatusMessageElement) currentStatusMessageElement.remove();
                        if (!finalResultReceived) addMessage("La risposta dal server sembra incompleta.", 'bot', null, true);
                        enableFullUI(true);
                        return;
                    }

                    buffer += decoder.decode(value, { stream: true });
                    const events = buffer.split('\n\n');
                    buffer = events.pop();

                    events.forEach(eventString => {
                        if (!eventString.trim()) return;

                        let eventType = 'message'; let eventDataString = '';
                        eventString.split('\n').forEach(line => {
                            if (line.startsWith('event:')) eventType = line.substring(6).trim();
                            else if (line.startsWith('data:')) eventDataString += line.substring(5);
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
                                if (currentStatusMessageElement) currentStatusMessageElement.remove();
                                if (eventData.performance_metrics) receivedPerformanceMetrics = eventData.performance_metrics;

                                if (eventData.success && eventData.answer) {
                                    if (eventData.answer.startsWith("BLOCKED:")) {
                                        const reason = eventData.answer.split(":", 2)[1] || "Ragione sconosciuta";
                                        addMessage(`⚠️ Risposta bloccata (${reason}). Riprova.`, 'bot', null, true, false, receivedPerformanceMetrics);
                                    } else {
                                        const rawHtml = marked.parse(eventData.answer);
                                        const sanitizedHtml = DOMPurify.sanitize(rawHtml);
                                        addMessage(sanitizedHtml, 'bot', eventData.retrieved_results, false, false, receivedPerformanceMetrics);
                                        manageChatHistory({ role: "assistant", content: eventData.answer });
                                    }
                                } else {
                                    addMessage(eventData.message || `Errore: ${eventData.error_code || 'Sconosciuto'}`, 'bot', null, true, false, receivedPerformanceMetrics);
                                }
                            }
                        } catch (e) {
                            console.error("Errore parsing JSON da evento SSE:", e, "Stringa evento:", JSON.stringify(eventDataString));
                        }
                    });
                    processStreamChunk();
                }).catch(streamError => {
                    console.error('Errore lettura stream SSE:', streamError);
                    if (currentStatusMessageElement) currentStatusMessageElement.remove();
                    addMessage(`Errore di comunicazione con il server: ${streamError.message}`, 'bot', null, true);
                    enableFullUI(true);
                });
            }
            processStreamChunk();

        } catch (error) {
            console.error('Errore invio query o gestione risposta iniziale:', error);
            if (currentStatusMessageElement) currentStatusMessageElement.remove();
            let errorMessage = `Si è verificato un errore: ${error.message}`;
            if (error instanceof TypeError) { 
                errorMessage = "Il server ha impiegato troppo tempo a rispondere. Potrebbe essere sovraccarico. Riprova la tua domanda.";
            }
            addMessage(errorMessage, 'bot', null, true);
            enableFullUI(true);
        }
    }

// --- FUNZIONE PER GENERARE IDEE DI CONTENUTI (RIFATTORIZZATA) ---
async function handleGenerateIdeas() {
    // Se siamo già in un contesto in cui la UI impedisce le chiamate esterne,
    // rispettiamo comunque lo stato, ma non dipendiamo dallo stato del singolo
    // pulsante lampadina (ideaGeneratorBtn) perché in modalità "idea" quella
    // lampadina viene nascosta/disabilitata mentre il pulsante "Altre" è il sendButton.
    if (typeof currentMode !== 'undefined' && currentMode === 'idea' && sendButton && sendButton.disabled) {
        return; // se il sendButton è disabilitato, non facciamo nulla
    }

    // Non bloccare la generazione semplicemente perché ideaGeneratorBtn è disabilitato.
    // Questo permette al pulsante "Altre" (sendButton in idea-mode) di funzionare correttamente.
    enableFullUI(false); 
    let thinkingMessage = addMessage("Sto cercando l'ispirazione...", 'bot', null, false, true);
    
    try {
        const response = await fetch('/api/ideas/generate');
        // Controlliamo anche lo status HTTP
        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.error || `Errore server: ${response.status}`);
        }
        const data = await response.json();

        if (thinkingMessage) thinkingMessage.remove();

        const rawHtml = marked.parse(data.ideas || data.ideas_html || '');
        const sanitizedHtml = DOMPurify.sanitize(rawHtml);
        
        addMessage(sanitizedHtml, 'bot');

        // Mettiamo la UI in modalità idea (assicurati che l'UI rifletta questo stato)
        currentMode = 'idea';
        userInput.value = ''; // Pulisci l'input per mostrare lo stato "Altre"
        userInput.dispatchEvent(new Event('input', { bubbles: true })); // Aggiorna lo stato UI
        userInput.focus();

    } catch (error) {
        console.error("Errore durante la generazione di idee:", error);
        if (thinkingMessage) thinkingMessage.remove();
        addMessage(`Si è verificato un errore: ${error.message}`, 'bot', null, true);
    } finally {
        enableFullUI(true); // Riabilita sempre l'interfaccia
    }
}

    // --- ASCOLTATORI DI EVENTI (INIZIALIZZAZIONE) ---

    // Quando l'utente scrive nella textarea: autosize e aggiorna lo stato dei pulsanti
    userInput.addEventListener('input', () => {
        userInput.style.height = 'auto'; 
        userInput.style.height = (userInput.scrollHeight) + 'px';
        updateUIState(); 
    });

    sendButton.addEventListener('click', handleSendMessage); // Click sul pulsante "Invia"
    ideaGeneratorBtn.addEventListener('click', handleGenerateIdeas); // Click sul pulsante "Lampadina"

    // Click sul pulsante "X" per uscire dalla modalità idea
    exitIdeaModeBtn.addEventListener('click', () => {
        currentMode = 'chat'; // Torna alla modalità chat
        userInput.value = ''; // Pulisci l'input
        userInput.dispatchEvent(new Event('input', { bubbles: true })); // Aggiorna lo stato UI
        userInput.focus();
    });

    // Pressione del tasto Invio: invia solo se c'è testo e il pulsante è abilitato
    userInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) { // Invio senza Shift
            event.preventDefault(); 
            // In modalità 'idea', Invio richiama handleGenerateIdeas
            if (currentMode === 'idea') {
                handleGenerateIdeas();
            } else {
                // In modalità 'chat' normale, Invio manda il messaggio solo se c'è testo
                if (userInput.value.trim().length > 0) {
                    handleSendMessage();
                } else {
                    // Se l'input è vuoto in modalità chat normale e premo Invio,
                    // non succede nulla (gestito da updateUIState che disabilita Invio)
                }
            }
        }
    });

    // --- INIZIALIZZAZIONE ---
    userInput.focus(); 
    updateUIState(); // Imposta lo stato iniziale corretto dei pulsanti (lampadina visibile, invia nascosto)
    setupPromptStarters(); // Inizializza i pulsanti prompt starter (se presenti)
});