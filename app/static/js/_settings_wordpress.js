// File: _settings_wordpress.js

document.addEventListener("DOMContentLoaded", function () {
    const syncBtn = document.getElementById("sync-wordpress-btn");
    const syncMessageDiv = document.getElementById("sync-message");
    const syncLoader = document.getElementById("sync-loader"); 

    if (syncBtn && syncMessageDiv) {
        const syncBtnOriginalHTML = syncBtn.innerHTML;

        syncBtn.addEventListener("click", async () => {
            const userConfirmed = await showConfirmModal(
                "Conferma sincronizzazione",
                "Assicurati di aver salvato le impostazioni. L'operazione potrebbe richiedere alcuni minuti. Vuoi avviare la sincronizzazione?"
            );
            if (!userConfirmed) return;

            // Avviamo la richiesta per far partire il processo in background
            try {
                const response = await fetch("/api/website/wordpress/sync", {
                    method: "POST"
                });
                const data = await response.json();

                // Se la richiesta di avvio fallisce (es. 400 Bad Request), mostriamo l'errore e ci fermiamo.
                if (!response.ok) {
                    throw new Error(data.message || "Impossibile avviare il processo di sincronizzazione.");
                }

                // Se l'avvio è andato a buon fine (risposta 202 Accepted),
                // deleghiamo tutto al nostro sistema di polling globale!
                window.startAsyncTask('/api/website/wordpress/progress', {
                    messageDiv: syncMessageDiv,
                    button: syncBtn,
                    loader: syncLoader,
                    buttonOriginalHTML: syncBtnOriginalHTML,
                    // Definiamo cosa fare quando il processo finisce con successo
                    onSuccess: () => {
                        // Attendi un istante per far leggere il messaggio finale,
                        // poi ricarica la pagina per mostrare i nuovi dati.
                        setTimeout(() => {
                            window.location.href = window.location.pathname + '#sito-web';
                            window.location.reload();
                        }, 1500);
                    }
                });

            } catch (error) {
                syncMessageDiv.className = "error-message";
                syncMessageDiv.textContent = `Errore: ${error.message}`;
                syncBtn.disabled = false;
                syncBtn.innerHTML = syncBtnOriginalHTML;
                if (typeof window.setAppStatus === "function") {
                    window.setAppStatus("ready");
                }
            }
        });
    }
});