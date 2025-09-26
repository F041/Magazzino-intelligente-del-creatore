document.addEventListener("DOMContentLoaded", function () {
    // --- SELEZIONE DEL FORM PRINCIPALE ---
    const settingsMainForm = document.getElementById("settings-main-form"); // <--- NUOVA RIGA
    if (!settingsMainForm) { // Controllo per robustezza
        console.error("Errore: Il form principale delle impostazioni non è stato trovato!");
        return; // Interrompi lo script se il form non c'è
    }

    // --- GESTIONE TAB ---
    const tabLinks = document.querySelectorAll(".tab-link");
    const tabContents = document.querySelectorAll(".tab-content");
    const formActionsDiv = document.querySelector(".form-actions");

    function activateTab(tabId) {
      tabLinks.forEach((link) => {
        link.classList.toggle("active", link.dataset.tab === tabId);
      });
      tabContents.forEach((content) => {
        content.classList.toggle("active", content.id === tabId);
      });
      localStorage.setItem("activeSettingsTab", tabId);

      if (formActionsDiv) {
        if (tabId === "protezione") {
          formActionsDiv.style.display = "none";
        } else {
          formActionsDiv.style.display = "flex";
        }
      }
    }

    const urlHash = window.location.hash.substring(1);
    const savedTab = localStorage.getItem("activeSettingsTab");
    let initialTabId = "generali";
    if (urlHash && document.getElementById(urlHash)) {
      initialTabId = urlHash;
    } else if (savedTab && document.getElementById(savedTab)) {
      initialTabId = savedTab;
    }
    tabLinks.forEach((el) => el.classList.remove("active"));
    tabContents.forEach((el) => el.classList.remove("active"));
    activateTab(initialTabId);

    tabLinks.forEach((link) => {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        const tabId = link.dataset.tab;
        activateTab(tabId);
      });
    });

    // --- LOGICA MENU A TENDINA E VISIBILITA' PANNELLI ---
    const customSelectContainer = document.querySelector(".custom-select-container");
    if (customSelectContainer) {
        const selectedDisplay = customSelectContainer.querySelector(".select-selected");
        const optionsList = customSelectContainer.querySelector(".select-items");
        const options = optionsList.querySelectorAll("div[data-value]");
        const hiddenInput = document.getElementById("llm_provider");
        const googleSettings = document.getElementById("google-settings-group");
        const groqSettings = document.getElementById("groq-settings-group");
        const ollamaSettings = document.getElementById("ollama-settings-group");
        const resetBtn = document.getElementById("reset-ai-settings-btn");

        function toggleResetButtonVisibility() {
          if (!resetBtn || !hiddenInput) return;
          const selectedProvider = hiddenInput.value;
          if (selectedProvider !== "google") {
            resetBtn.style.display = "inline-flex";
          } else {
            resetBtn.style.display = "none";
          }
        }

        function toggleProviderSettings() {
          if (!hiddenInput) return;
          const selectedProvider = hiddenInput.value;
          if (googleSettings) {
            googleSettings.classList.toggle("visible", selectedProvider === "google");
            googleSettings.classList.toggle("hidden", selectedProvider !== "google");
          }
          if (groqSettings) {
          groqSettings.classList.toggle("visible", selectedProvider === "groq");
          groqSettings.classList.toggle("hidden", selectedProvider !== "groq");
          }
          if (ollamaSettings) {
            ollamaSettings.classList.toggle("visible", selectedProvider === "ollama");
            ollamaSettings.classList.toggle("hidden", selectedProvider !== "ollama");
          }
          toggleResetButtonVisibility();
        }

        if (selectedDisplay) {
            selectedDisplay.addEventListener("click", function (e) {
              e.stopPropagation();
              optionsList.classList.toggle("select-hide");
              this.classList.toggle("select-arrow-active");
            });
        }

        options.forEach((option) => {
          option.addEventListener("click", function () {
            if (selectedDisplay && hiddenInput && optionsList) {
                selectedDisplay.innerHTML = this.innerHTML;
                hiddenInput.value = this.getAttribute("data-value");
                optionsList.classList.add("select-hide");
                selectedDisplay.classList.remove("select-arrow-active");
                toggleProviderSettings();
            }
          });
        });

        document.addEventListener("click", function () {
            if (optionsList && selectedDisplay) {
                optionsList.classList.add("select-hide");
                selectedDisplay.classList.remove("select-arrow-active");
            }
        });

        toggleProviderSettings();
    }


    // --- LOGICA DI SINCRONIZZAZIONE WORDPRESS ---
    const syncBtn = document.getElementById("sync-wordpress-btn");
    const syncMessageDiv = document.getElementById("sync-message");
    let pollingInterval = null;
    if (syncBtn && syncMessageDiv) {
      const syncBtnOriginalHTML = syncBtn.innerHTML;
      async function checkWpProgress() {
        try {
          const response = await fetch("/api/website/wordpress/progress");
          const data = await response.json();
          if (!data.is_processing) {
            clearInterval(pollingInterval);
            pollingInterval = null;
            syncMessageDiv.className = data.error
              ? "error-message"
              : "success-message";
            syncMessageDiv.textContent =
              data.error || data.message || "Operazione completata.";
            if (typeof window.setAppStatus === "function") {
              window.setAppStatus("ready");
            }
            if (!data.error) {
              setTimeout(() => {
                window.location.reload();
              }, 1500);
            } else {
              syncBtn.disabled = false;
              syncBtn.innerHTML = syncBtnOriginalHTML;
            }
            } else {
                    syncMessageDiv.className = 'info-message';
                    syncMessageDiv.textContent = data.message; 
                }
        } catch (error) {
          clearInterval(pollingInterval);
          pollingInterval = null;
          syncMessageDiv.className = "error-message";
          syncMessageDiv.textContent =
            "Errore di connessione durante il controllo dello stato.";
          syncBtn.disabled = false;
          syncBtn.innerHTML = syncBtnOriginalHTML;
          if (typeof window.setAppStatus === "function") {
            window.setAppStatus("ready");
          }
        }
      }
  syncBtn.addEventListener("click", async () => {
    const userConfirmed = await showConfirmModal(
      "Conferma sincronizzazione",
      "Assicurati di aver salvato le impostazioni. L'operazione potrebbe richiedere alcuni minuti. Vuoi avviare la sincronizzazione?"
    );
    if (!userConfirmed) return;

    // --- INIZIO LOGICA MODIFICATA ---
    
    // 1. Avvia la richiesta per far partire il processo in background
    try {
        const response = await fetch("/api/website/wordpress/sync", {
            method: "POST"
        });
        const data = await response.json();
        if (!response.ok) { // Se la richiesta di avvio fallisce, mostra l'errore e fermati
            throw new Error(data.message || "Impossibile avviare il processo di sincronizzazione.");
        }

        // 2. Se l'avvio è andato a buon fine (202 Accepted), DELEGA tutto al sistema avanzato
        // La funzione globale window.startAsyncTask è definita in base.html
        window.startAsyncTask('/api/website/wordpress/progress', {
            messageDiv: syncMessageDiv,
            button: syncBtn,
            buttonOriginalHTML: syncBtnOriginalHTML,
            onSuccess: () => {
                // Attendi un istante per far leggere il messaggio finale,
                // poi ricarica la pagina all'ancora corretta.
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
    // --- FINE LOGICA MODIFICATA ---
  });
    }

    // --- LOGICA PER IL TEST DI CONNESSIONE OLLAMA ---
    const testOllamaBtn = document.getElementById("test-ollama-btn");
    const ollamaUrlInput = document.getElementById("ollama_base_url");
    const ollamaModelInput = document.getElementById("ollama_model_name");
    const ollamaResultDiv = document.getElementById("ollama-test-result");
    let ollamaConnectionVerified = false;

    if (testOllamaBtn && ollamaUrlInput && ollamaModelInput && ollamaResultDiv) {
        function updateTestButtonState() {
          if (ollamaConnectionVerified) {
            testOllamaBtn.style.background = "var(--color-success)";
            testOllamaBtn.innerHTML =
              '<i class="fas fa-check"></i> <span>Connesso</span>';
            testOllamaBtn.disabled = true;
          } else {
            testOllamaBtn.style.background = "var(--color-text-light)";
            testOllamaBtn.innerHTML = "Testa connessione";
            testOllamaBtn.disabled = false;
          }
        }

        function resetConnectionStatus() {
          if (ollamaConnectionVerified) {
            ollamaConnectionVerified = false;
            updateTestButtonState();
            ollamaResultDiv.style.display = "none";
          }
        }
        ollamaUrlInput.addEventListener("input", resetConnectionStatus);
        ollamaModelInput.addEventListener("input", resetConnectionStatus);

        testOllamaBtn.addEventListener("click", async () => {
            const url = ollamaUrlInput.value.trim();
            const model = ollamaModelInput.value.trim();
            if (!url || !model) {
              alert(
                "Per favore, inserisci sia l-URL del server che il nome del modello prima di testare."
              );
              return;
            }
            testOllamaBtn.disabled = true;
            testOllamaBtn.innerHTML =
              '<i class="fas fa-spinner fa-spin"></i> <span>Test in corso...</span>';
            ollamaResultDiv.style.display = "none";
            try {
              const response = await fetch("/api/settings/test_ollama", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ollama_url: url, model_name: model }),
              });
              const data = await response.json();
              ollamaResultDiv.textContent = data.message;
              if (data.success) {
                ollamaResultDiv.className = "success-message";
                ollamaConnectionVerified = true;
              } else {
                ollamaResultDiv.className = "error-message";
                ollamaConnectionVerified = false;
              }
              ollamaResultDiv.style.display = "block";
            } catch (error) {
              ollamaResultDiv.textContent =
                "Errore di rete imprevisto durante il test.";
              ollamaResultDiv.className = "error-message";
              ollamaConnectionVerified = false;
            } finally {
              updateTestButtonState();
            }
        });
    }

    // --- LOGICA PER IL PULSANTE DI RIPRISTINO IMPOSTAZIONI AI ---
    const resetBtnAI = document.getElementById("reset-ai-settings-btn");
    if (resetBtnAI) {
      resetBtnAI.addEventListener("click", async () => {
        const userConfirmed = await showConfirmModal(
          "Conferma ripristino",
          "Sei sicuro di voler ripristinare le impostazioni AI ai valori predefiniti? Le personalizzazioni correnti verranno perse."
        );
        if (!userConfirmed) return;

        resetBtnAI.disabled = true;
        resetBtnAI.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

        try {
          const response = await fetch("/api/settings/reset_ai", {
            method: "POST",
          });
          const data = await response.json();
          if (!response.ok)
            throw new Error(data.message || "Errore del server");

          showNotification(
            "Impostazioni ripristinate con successo! La pagina verrà ricaricata.",
            "success"
          );

          setTimeout(() => {
            window.location.reload();
          }, 1500);
        } catch (error) {
          showNotification(
            `Errore durante il ripristino: ${error.message}`,
            "error"
          );
          resetBtnAI.disabled = false;
          resetBtnAI.innerHTML =
            '<i class="fas fa-undo"></i> Ripristina predefinite';
        }
      });
    }

    // --- WORKAROUND PER AUTOCOMPLETE AGGRESSIVO SUL CAMPO API KEY ---
    const apiKeyInput = document.getElementById('llm_api_key');
    if (apiKeyInput) {
        apiKeyInput.addEventListener('focus', function() {
            this.removeAttribute('readonly');
        });
        apiKeyInput.addEventListener('blur', function() {
            if (this.value === '') {
                this.setAttribute('readonly', true);
            }
        });
    }

    // --- LOGICA PER IL RIPRISTINO DA BACKUP ---
    const restoreBtn = document.getElementById('restore-backup-btn');
    const fileInput = document.getElementById('backup-file-input');
    const restoreStatusDiv = document.getElementById('restore-status');

    if (restoreBtn && fileInput && restoreStatusDiv) {
        // Preveniamo la submission del form principale quando si clicca questo pulsante
        restoreBtn.addEventListener('click', (e) => {
            e.preventDefault(); // <--- ESSENZIALE: Blocca la submission del form!
            fileInput.click();
        });

        fileInput.addEventListener('change', async (event) => {
            event.preventDefault(); 
            const file = event.target.files[0];
            if (!file) {
                fileInput.value = ''; 
                return;
            }

            const confirmed = await showConfirmModal(
                "Conferma Ripristino Dati",
                `Sei assolutamente sicuro di voler ripristinare il database con il file "${file.name}"? Tutti i dati attuali verranno sovrascritti.`
            );
            
            if (!confirmed) {
                fileInput.value = '';
                return;
            }

            const formData = new FormData();
            formData.append('backup_file', file);
            
            restoreBtn.disabled = true;
            restoreStatusDiv.className = 'info-message';
            restoreStatusDiv.textContent = 'Caricamento del file di backup in corso...';
            restoreStatusDiv.style.display = 'block';

            try {
                const response = await fetch('/api/protection/restore/database', {
                    method: 'POST',
                    body: formData
                });

                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.message || 'Errore del server durante il caricamento.');
                }

                if (response.status === 202) {
                    showNotification('Ripristino avviato! Ora parte la re-indicizzazione, potrebbe volerci un po\'.', 'success');
                    
                    window.startAsyncTask('/api/protection/reindex-progress', {
                        messageDiv: restoreStatusDiv,
                        button: restoreBtn,
                        buttonOriginalHTML: '<i class="fas fa-upload"></i> Carica e Ripristina Database',
                        onSuccess: () => {
                            // Quando il processo in background finisce con successo, ricarica la pagina.
                            setTimeout(() => {
                                window.location.href = window.location.pathname + '#protezione';
                                window.location.reload();
                            }, 1500); // Attendi 1.5 secondi per far leggere il messaggio finale
                        }
                    });

                } else {
                     restoreStatusDiv.className = 'success-message';
                     restoreStatusDiv.textContent = data.message;
                     restoreBtn.disabled = false;
                }

            } catch (error) {
                restoreStatusDiv.className = 'error-message';
                restoreStatusDiv.textContent = `Errore: ${error.message}`;
                restoreBtn.disabled = false;
            } finally {
                fileInput.value = '';
            }
        });
    }

    // --- GESTIONE DELLA SUBMISSION DEL FORM PRINCIPALE ---
    // Questo gestore previene l'invio del form a meno che non sia il pulsante "Salva impostazioni"
    settingsMainForm.addEventListener('submit', function(e) { // <--- NUOVO LISTENER AL FORM PRINCIPALE
        // Controlla se il pulsante che ha scatenato la submission è "Salva impostazioni"
        // In questo caso, e.submitter è il pulsante cliccato.
        const submitButton = e.submitter;
        if (submitButton && submitButton.type === 'submit' && submitButton.textContent.includes('Salva impostazioni')) {
            // Se è il pulsante "Salva impostazioni", lascia che il form si invii normalmente
            // console.log("Form submitted by 'Salva impostazioni' button.");
            return true;
        } else {
            // Per qualsiasi altra interazione (es. Invio involontario, click su altri pulsanti non-submit)
            // blocca la submission del form.
            e.preventDefault(); // <--- ESSENZIALE
            // console.log("Form submission prevented for non-'Salva impostazioni' action.");
            return false;
        }
    });

    // Funzione helper per le notifiche (se non è già in base.html)
    function showNotification(message, type = "success") {
        const container =
            document.querySelector(".tabs-container") || document.body;
        let messageDiv = document.getElementById("dynamic-notification");
        if (!messageDiv) {
            messageDiv = document.createElement("div");
            messageDiv.id = "dynamic-notification";
            container.prepend(messageDiv);
        }
        messageDiv.className =
            type === "success" ? "success-message" : "error-message";
        messageDiv.textContent = message;
        messageDiv.style.marginBottom = "15px";
    }
        // --- LOGICA PER IL RIPRISTINO COMPLETO DA BACKUP (.zip) ---
    const restoreFullBtn = document.getElementById('restore-full-backup-btn');
    const fileInputFull = document.getElementById('full-backup-file-input');
    const restoreFullStatusDiv = document.getElementById('restore-full-status');

    if (restoreFullBtn && fileInputFull && restoreFullStatusDiv) {
        restoreFullBtn.addEventListener('click', (e) => {
            e.preventDefault();
            fileInputFull.click();
        });

        fileInputFull.addEventListener('change', async (event) => {
            event.preventDefault();
            const file = event.target.files[0];
            if (!file) {
                fileInputFull.value = '';
                return;
            }

            const confirmed = await showConfirmModal(
                "Conferma Ripristino COMPLETO",
                `Stai per caricare il file "${file.name}". Tutti i dati attuali verranno SOVRASCRITTI. L'applicazione dovrà essere riavviata per completare il processo. Sei assolutamente sicuro?`
            );
            
            if (!confirmed) {
                fileInputFull.value = '';
                return;
            }

            const formData = new FormData();
            formData.append('backup_file', file);
            
            restoreFullBtn.disabled = true;
            restoreFullStatusDiv.className = 'info-message';
            restoreFullStatusDiv.textContent = 'Caricamento del file di backup completo in corso...';
            restoreFullStatusDiv.style.display = 'block';

            try {
                const response = await fetch('/api/protection/restore/full', {
                    method: 'POST',
                    body: formData
                });

                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.message || 'Errore del server durante il caricamento.');
                }

                if (response.status === 202) {
                    restoreFullStatusDiv.className = 'success-message';
                    restoreFullStatusDiv.innerHTML = `<strong>${data.message}</strong><br>Se usi Docker, il container si riavvierà automaticamente tra poco. Altrimenti, riavvia manualmente il server.`;
                    showNotification('File caricato! Riavvia l\'applicazione per completare.', 'success');
                } else {
                     restoreFullStatusDiv.className = 'success-message';
                     restoreFullStatusDiv.textContent = data.message;
                     restoreFullBtn.disabled = false;
                }

            } catch (error) {
                restoreFullStatusDiv.className = 'error-message';
                restoreFullStatusDiv.textContent = `Errore: ${error.message}`;
                restoreFullBtn.disabled = false;
            } finally {
                fileInputFull.value = '';
            }
        });
    }
        const downloadFullBtn = document.getElementById('download-full-backup-btn');
    if (downloadFullBtn) {
        downloadFullBtn.addEventListener('click', function() {
            const button = this;
            const originalHTML = button.innerHTML;
            const downloadUrl = button.dataset.url;

            if (!downloadUrl) return;

            // 1. Disabilita il pulsante e mostra lo spinner
            button.disabled = true;
            button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> <span>Preparazione...</span>';

            // 2. Avvia il download
            window.location.href = downloadUrl;

            // 3. Ripristina il pulsante dopo un breve ritardo.
            // Il browser gestirà il download, noi dobbiamo solo ripristinare l'UI.
            setTimeout(() => {
                button.disabled = false;
                button.innerHTML = originalHTML;
            }, 4000); // 4 secondi sono sufficienti perché il download parta
        });
    }

});