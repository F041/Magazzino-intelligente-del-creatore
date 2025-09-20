// FILE: app/static/js/chat.js

document.addEventListener('DOMContentLoaded', () => {
    let JWT_TOKEN = null; 
    
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
    JWT_TOKEN = getTokenFromUrl(); 
    
    console.log(`Chat JS loaded. Token JWT presente: ${JWT_TOKEN ? 'Sì' : 'No'}`);

    let chatHistory = [];
    const MAX_HISTORY_MESSAGES_TO_SEND = 6;
    const MAX_LOCAL_HISTORY_ITEMS = 50;

    const chatWindow = document.getElementById('messages-list'); 
    const userInput = document.getElementById('user-input');
    const sendButton = document.getElementById('send-button');
    const promptStartersContainer = document.getElementById('prompt-starters');

    if (!chatWindow || !userInput || !sendButton) {
        console.error("Elementi base della chat non trovati!");
        return;
    }

    function setupPromptStarters() {
        if (!promptStartersContainer) return;

        promptStartersContainer.querySelectorAll('.prompt-starter-btn').forEach((button, index) => {
            button.addEventListener('click', () => {
                const promptText = button.textContent.trim();
                console.log("[prompt-starter] click. index:", index, "promptText:", JSON.stringify(promptText));

                userInput.value = promptText;
                userInput.dispatchEvent(new Event('input', { bubbles: true }));
                userInput.focus();

                if (typeof handleSendMessage === 'function') {
                    window.handleSendMessage = handleSendMessage;
                }

                Promise.resolve().then(() => {
                    enableUI(true);
                    if (!sendButton.disabled) {
                        sendButton.click();
                        setTimeout(() => {
                            if (typeof handleSendMessage === 'function' && !sendButton.disabled) {
                                console.log("[prompt-starter] fallback: chiamo handleSendMessage() per index", index);
                                handleSendMessage();
                            }
                        }, 50);
                    } else {
                        console.log("[prompt-starter] sendButton è disabilitato, chiamo handleSendMessage() direttamente per index", index);
                        if (typeof handleSendMessage === 'function') handleSendMessage();
                    }
                });
            });
        });
    }

    function manageChatHistory(newEntry) {
        chatHistory.push(newEntry);
        if (chatHistory.length > MAX_LOCAL_HISTORY_ITEMS) { 
            chatHistory = chatHistory.slice(-MAX_LOCAL_HISTORY_ITEMS); 
            console.log(`Cronologia locale troncata a ${MAX_LOCAL_HISTORY_ITEMS} messaggi.`); 
        }
    }

    // --- FUNZIONE AGGIORNATA addMessage per includere le metriche ---
    function addMessage(content, sender, references = null, isError = false, isLoading = false, performanceMetrics = null) {
        const messageDiv = document.createElement('div');
        messageDiv.classList.add('message', `${sender}-message`);
        if (isError) messageDiv.classList.add('error');
        if (isLoading) messageDiv.classList.add('loading');

        if (content && typeof content === 'string' && content.trim() !== '') {
            const paragraph = document.createElement('p');
            if (sender === 'bot' && !isLoading && !isError) {
                paragraph.innerHTML = content; 
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

        // --- INIZIO BLOCCO METRICHE DI PERFORMANCE ---
        if (sender === 'bot' && performanceMetrics && !isError && !isLoading) {
            const metricsContainer = document.createElement('div');
            metricsContainer.classList.add('metrics-container');
            metricsContainer.style.display = 'none'; // Nascosto di default

            const metricsToggle = document.createElement('button');
            metricsToggle.classList.add('metrics-toggle');
            metricsToggle.textContent = 'Dettagli Performance ▼';
            metricsToggle.addEventListener('click', function() {
                const isHidden = metricsContainer.style.display === 'none';
                metricsContainer.style.display = isHidden ? 'block' : 'none';
                metricsToggle.textContent = `Dettagli Performance ${isHidden ? '▲' : '▼'}`;
            });

            // Funzione helper per creare una riga metrica
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
                        bar.style.width = `${Math.min(percentage, 100)}%`; // Non superare 100%
                        bar.style.backgroundColor = barColor;
                        barContainer.appendChild(bar);
                        p.appendChild(barContainer);
                    }
                } else {
                    p.innerHTML = `<strong>${label}:</strong> ${displayValue}`;
                }
                return p;
            }

            // Aggiungi le metriche una per una
            const totalDuration = performanceMetrics.total_duration_ms;

            metricsContainer.appendChild(createMetricRow('Totale', totalDuration, totalDuration, 'var(--color-secondary)'));
            metricsContainer.appendChild(createMetricRow('Embedding', performanceMetrics.embedding_duration_ms, totalDuration, '#4cc9f0'));
            metricsContainer.appendChild(createMetricRow('Ricerca Vettoriale', performanceMetrics.retrieval_duration_ms, totalDuration, '#4895ef'));
            
            // Re-ranking è opzionale, lo mostriamo solo se eseguito
            if (performanceMetrics.reranking_duration_ms !== undefined && performanceMetrics.reranking_duration_ms > 0) {
                metricsContainer.appendChild(createMetricRow('Re-ranking Cohere', performanceMetrics.reranking_duration_ms, totalDuration, '#f72585'));
            }
            
            metricsContainer.appendChild(createMetricRow('Generazione LLM', performanceMetrics.llm_generation_duration_ms, totalDuration, '#7209b7'));
            metricsContainer.appendChild(createMetricRow('Modello LLM usato', performanceMetrics.llm_model_used));
            metricsContainer.appendChild(createMetricRow('Chunk recuperati', performanceMetrics.retrieved_chunks_count));

            messageDiv.appendChild(metricsToggle);
            messageDiv.appendChild(metricsContainer);
        }
        // --- FINE BLOCCO METRICHE DI PERFORMANCE ---

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

        if (promptStartersContainer) {
        promptStartersContainer.style.display = 'none';
        }

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
            // --- NUOVA VARIABILE PER METRICHE ---
            let receivedPerformanceMetrics = null; 

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
                                // --- LEGGI LE METRICHE DAL PAYLOAD FINALE ---
                                if (eventData.performance_metrics) {
                                    receivedPerformanceMetrics = eventData.performance_metrics;
                                    console.log("Metriche di Performance ricevute:", receivedPerformanceMetrics);
                                }
                                // --- FINE BLOCCO METRICHE ---

                                if (eventData.success && eventData.answer) {
                                    if (eventData.answer.startsWith("BLOCKED:")) {
                                        const reason = eventData.answer.split(":", 2)[1] || "Ragione sconosciuta";
                                        addMessage(`⚠️ Risposta bloccata (${reason}). Riprova.`, 'bot', null, true, false, receivedPerformanceMetrics);
                                    } else {
                                        const rawHtml = marked.parse(eventData.answer);
                                        const sanitizedHtml = DOMPurify.sanitize(rawHtml);
                                        // PASSA LE METRICHE A addMessage
                                        addMessage(sanitizedHtml, 'bot', eventData.retrieved_results, false, false, receivedPerformanceMetrics);
   
                                        manageChatHistory({ role: "assistant", content: eventData.answer });                                    }
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
            let errorMessage = `Si è verificato un errore: ${error.message}`;
            if (error instanceof TypeError) { 
                errorMessage = "Il server ha impiegato troppo tempo a rispondere. Potrebbe essere sovraccarico. Riprova la tua domanda.";
            }
            addMessage(errorMessage, 'bot', null, true);
            enableUI(true);
        }
    }

    userInput.addEventListener('input', () => {
    userInput.style.height = 'auto'; 
    userInput.style.height = (userInput.scrollHeight) + 'px'; 
    });

    sendButton.addEventListener('click', handleSendMessage);
    userInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            handleSendMessage();
        }
    });
    
    userInput.focus();
    setupPromptStarters();   
});