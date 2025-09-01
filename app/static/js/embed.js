(function() {
    // --- 1. CONFIGURAZIONE E AUTENTICAZIONE JWT ---
    const scriptTag = document.currentScript;
    const customerId = scriptTag.getAttribute('data-customer-id');
    const magazzinoBaseUrl = new URL(scriptTag.src).origin;
    let jwtToken = null, isFetchingToken = false, tokenFetchPromise = null;

    async function fetchJwtToken() {
        if (!customerId) { console.error("Magazzino Widget: data-customer-id mancante."); return null; }
        try {
            const response = await fetch(`${magazzinoBaseUrl}/keys/api/public/generate-widget-token`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ customerId: customerId }) });
            const data = await response.json();
            if (response.ok && data.token) { console.log("Magazzino Widget: Token JWT ottenuto."); return data.token; } 
            else { console.error("Magazzino Widget: Errore ottenimento token JWT.", data.message); return null; }
        } catch (error) { console.error("Magazzino Widget: Errore di rete.", error); return null; }
    }
    async function getValidToken() {
        if (jwtToken) return jwtToken;
        if (isFetchingToken) return tokenFetchPromise;
        isFetchingToken = true;
        tokenFetchPromise = fetchJwtToken();
        jwtToken = await tokenFetchPromise;
        isFetchingToken = false;
        return jwtToken;
    }

    // --- 2. LOGICA DI INVIO MESSAGGIO (CON FETCH SSE) ---
    async function handleSendMessage() {
        const userInput = document.getElementById('magazzino-user-input');
        const sendButton = document.getElementById('magazzino-send-button');
        const query = userInput.value.trim();
        if (!query) return;
        addMessage(query, 'user');
        userInput.value = '';
        userInput.disabled = true;
        sendButton.disabled = true;
        const thinkingMessageId = `bot-msg-${Date.now()}`;
        addMessage("...", 'bot', false, thinkingMessageId);
        const token = await getValidToken();
        if (!token) {
            updateMessage("Errore di autenticazione.", thinkingMessageId, true);
            userInput.disabled = false;
            sendButton.disabled = false;
            return;
        }
        try {
            const response = await fetch(`${magazzinoBaseUrl}/api/search/`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'Authorization': `Bearer ${token}` },
                body: JSON.stringify({ query: query })
            });
            if (!response.ok) { throw new Error(`Errore HTTP: ${response.status}`); }
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n\n');
                buffer = lines.pop();
                for (const line of lines) {
                    try {
                        if (line.startsWith('event: status')) {
                            const jsonData = JSON.parse(line.split('data: ')[1]);
                            updateMessage(jsonData.message + "...", thinkingMessageId);
                        } else if (line.startsWith('event: result')) {
                            const jsonData = JSON.parse(line.split('data: ')[1]);
                            updateMessage(jsonData.answer, thinkingMessageId);
                        } else if (line.startsWith('event: error_final')) {
                            const jsonData = JSON.parse(line.split('data: ')[1]);
                            updateMessage(jsonData.message || "Errore.", thinkingMessageId, true);
                        }
                    } catch (e) { console.error("Errore parsing SSE:", e, "Linea:", line); }
                }
            }
        } catch (error) {
            updateMessage("Errore di connessione.", thinkingMessageId, true);
        } finally {
            userInput.disabled = false;
            sendButton.disabled = false;
            userInput.focus();
        }
    }

    // --- 3. CREAZIONE DELL'INTERFACCIA E GESTIONE UI ---
    function createChatWidget() {
        document.body.insertAdjacentHTML('beforeend', `
            <div id="magazzino-chat-widget" class="magazzino-widget-container">
                <div id="magazzino-chat-bubble" class="magazzino-bubble"><svg xmlns="http://www.w3.org/2000/svg" height="24px" viewBox="0 0 24 24" width="24px" fill="#FFFFFF"><path d="M0 0h24v24H0V0z" fill="none"/><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg></div>
                <div id="magazzino-chat-window" class="magazzino-window"><div class="magazzino-header">Conversa con noi</div><div class="magazzino-body" id="magazzino-chat-body"></div><div class="magazzino-footer"><textarea id="magazzino-user-input" placeholder="Chiedi qualcosa..."></textarea><button id="magazzino-send-button">Invia</button></div></div>
            </div>`);
        document.head.insertAdjacentHTML('beforeend', `<style>
            .magazzino-widget-container{position:fixed;bottom:20px;right:20px;z-index:9999;}.magazzino-bubble{width:60px;height:60px;background-color:#007bff;border-radius:50%;display:flex;justify-content:center;align-items:center;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,0.2);transition:transform .2s;}.magazzino-bubble:hover{transform:scale(1.1);}.magazzino-window{display:none;width:350px;max-width:90vw;height:500px;max-height:80vh;background:white;border-radius:10px;box-shadow:0 5px 20px rgba(0,0,0,0.2);flex-direction:column;}.magazzino-window.open{display:flex;}.magazzino-header{padding:15px;background:#007bff;color:white;border-top-left-radius:10px;border-top-right-radius:10px;font-family:sans-serif;}.magazzino-body{flex-grow:1;padding:15px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;}.magazzino-footer{display:flex;padding:10px;border-top:1px solid #eee;}.magazzino-footer textarea{flex-grow:1;border:1px solid #ccc;border-radius:20px;padding:10px;resize:none;font-family:sans-serif;font-size:1em;}.magazzino-footer button{margin-left:10px;padding:10px 15px;border:none;background:#007bff;color:white;border-radius:20px;cursor:pointer;}.magazzino-message{padding:8px 12px;border-radius:18px;max-width:85%;line-height:1.4;font-family:sans-serif;font-size:.95em;}.magazzino-user-message{background-color:#007bff;color:white;align-self:flex-end;margin-left:auto;border-bottom-right-radius:5px;}.magazzino-bot-message{background-color:#e9ecef;color:#333;align-self:flex-start;border-bottom-left-radius:5px;}.magazzino-bot-message.magazzino-error-message{background-color:#f8d7da;color:#721c24;}
        </style>`);
        const chatBubble = document.getElementById('magazzino-chat-bubble');
        const chatWindow = document.getElementById('magazzino-chat-window');
        const sendButton = document.getElementById('magazzino-send-button');
        const userInput = document.getElementById('magazzino-user-input');
        chatBubble.addEventListener('click', () => chatWindow.classList.toggle('open'));
        sendButton.addEventListener('click', handleSendMessage);
        userInput.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendMessage(); } });
    }
    function addMessage(text, type, isError = false, messageId = null) {
        const chatBody = document.getElementById('magazzino-chat-body');
        const messageDiv = document.createElement('div');
        if (messageId) messageDiv.id = messageId;
        messageDiv.className = `magazzino-message ${type === 'user' ? 'magazzino-user-message' : 'magazzino-bot-message'}`;
        if (isError) messageDiv.classList.add('magazzino-error-message');
        messageDiv.textContent = text;
        chatBody.appendChild(messageDiv);
        chatBody.scrollTop = chatBody.scrollHeight;
    }
    function updateMessage(newText, messageId, isError = false) {
        const messageDiv = document.getElementById(messageId);
        if (messageDiv) {
            messageDiv.textContent = newText;
            if (isError) messageDiv.classList.add('magazzino-error-message');
        }
    }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', createChatWidget); } else { createChatWidget(); }
})();