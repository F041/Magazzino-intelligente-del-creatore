// FILE: app/static/js/embed.js
(function() {
    console.log("Embed script executing...");

    // --- Configurazione ---
    // Recupera l'URL base dello script stesso per costruire gli altri URL
    const scriptTag = document.currentScript;
    if (!scriptTag) {
        console.error("Embed Error: Impossibile determinare l'URL base dallo script tag.");
        return;
    }
    // Estrai l'origine (es. http://localhost:5000) dall'URL dello script
    const scriptSrc = scriptTag.src;
    const BASE_URL = new URL(scriptSrc).origin;
    
    const apiKey = scriptTag.getAttribute('data-api-key');
    if (!apiKey) {
        console.warn("Embed Warning: Attributo 'data-api-key' non trovato o vuoto nello script tag. Il widget potrebbe non autenticarsi correttamente in modalità SAAS.");
        // Non blocchiamo l'esecuzione, ma il widget fallirà se serve la chiave
    } else {
        console.log("Embed: API Key trovata nello script tag.");
    }

    // Costruisci l'URL dell'iframe, aggiungendo l'apiKey se presente
    let widgetIframeSrc = `${BASE_URL}/widget`;
    if (apiKey) {
        // Aggiungi come parametro URL (attenzione: visibile nel DOM)
        widgetIframeSrc += `?apiKey=${encodeURIComponent(apiKey)}`;
        console.log("Embed: URL Iframe con API Key:", widgetIframeSrc);
    } else {
            console.log("Embed: URL Iframe senza API Key:", widgetIframeSrc);
    }

    const WIDGET_IFRAME_SRC = `${BASE_URL}/widget`;
    const BUTTON_ID = 'magazzino-chat-button';
    const WIDGET_ID = 'magazzino-chat-widget-container';
    const IFRAME_ID = 'magazzino-chat-widget-iframe';
    // Icone (semplici caratteri per ora)
    const ICON_CHAT = '?';
    const ICON_CLOSE = '×';

    // --- Stili CSS ---
    const css = `
        #${BUTTON_ID} {
            position: fixed; bottom: 20px; right: 20px; width: 60px; height: 60px;
            background-color: #007bff; color: white; border-radius: 50%; border: none;
            font-size: 28px; line-height: 60px; text-align: center; cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2); z-index: 9998;
            transition: transform 0.2s ease, background-color 0.2s ease;
        }
        #${BUTTON_ID}:hover { background-color: #0056b3; transform: scale(1.1); }
        #${BUTTON_ID}.widget-open::before { content: '${ICON_CLOSE}'; font-size: 32px; }
        #${BUTTON_ID}:not(.widget-open)::before { content: '${ICON_CHAT}'; font-weight: bold; }

        #${WIDGET_ID} {
            position: fixed; bottom: 90px; right: 20px; width: 380px; height: 600px;
            max-width: calc(100vw - 40px); max-height: calc(100vh - 110px);
            background-color: #fff; border: 1px solid #ccc; border-radius: 10px;
            box-shadow: 0 6px 20px rgba(0,0,0,0.2); overflow: hidden;
            display: none; z-index: 9999;
        }
        #${IFRAME_ID} { width: 100%; height: 100%; border: none; }

        /* Responsive per schermi piccoli */
        @media (max-width: 450px) {
            #${WIDGET_ID} { width: calc(100% - 30px); height: 75%; bottom: 80px; right: 15px; left: 15px; }
            #${BUTTON_ID} { width: 50px; height: 50px; line-height: 50px; font-size: 24px; bottom: 15px; right: 15px; }
            #${BUTTON_ID}.widget-open::before { font-size: 28px; }
        }
    `;

    // --- Logica Widget ---
    let widgetContainer = null;
    let iframe = null;
    let chatButton = null;
    let isWidgetOpen = false;

    function createWidgetElements() {
        if (document.getElementById(BUTTON_ID)) { console.warn("Embed script: Elementi già presenti."); return; }
        console.log("Embed: Creating widget elements...");
        const styleElement = document.createElement('style');
        styleElement.textContent = css;
        (document.head || document.documentElement).appendChild(styleElement);
        chatButton = document.createElement('button'); chatButton.id = BUTTON_ID; document.body.appendChild(chatButton);
        widgetContainer = document.createElement('div'); widgetContainer.id = WIDGET_ID; document.body.appendChild(widgetContainer);
        iframe = document.createElement('iframe'); iframe.id = IFRAME_ID;
        // --- USA L'URL CON API KEY (se presente) ---
        iframe.src = widgetIframeSrc;
        // ------------------------------------------
        iframe.title = "Chat Magazzino Creatore"; widgetContainer.appendChild(iframe);
        chatButton.addEventListener('click', toggleWidget);
        console.log("Embed: Widget elements created.");
    }

    function toggleWidget() {
        isWidgetOpen = !isWidgetOpen;
        if (widgetContainer && chatButton) {
            widgetContainer.style.display = isWidgetOpen ? 'block' : 'none';
            chatButton.classList.toggle('widget-open', isWidgetOpen); // Aggiunge/rimuove classe per icona
            // Potresti anche cambiare l'innerHTML del bottone qui se non usi ::before
            // chatButton.innerHTML = isWidgetOpen ? ICON_CLOSE : ICON_CHAT;
            console.log("Embed: Widget toggled. Open:", isWidgetOpen);
        } else {
            console.error("Embed: Container or button not found for toggle.");
        }
    }

    // Assicurati che il DOM sia pronto prima di creare gli elementi
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', createWidgetElements);
    } else {
        createWidgetElements(); // DOM già pronto
    }

})();