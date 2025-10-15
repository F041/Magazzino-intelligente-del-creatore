document.addEventListener("DOMContentLoaded", function () {
    // --- CHIAVI E LOGICA ACCORDION (CON MEMORIA) ---
    const ACCORDION_STORAGE_KEY = "magazzino_active_accordion";
    const accordionItems = document.querySelectorAll(".accordion-item");
    const lastActiveId = localStorage.getItem(ACCORDION_STORAGE_KEY);

    accordionItems.forEach((item) => {
      const header = item.querySelector(".accordion-header");
      const content = item.querySelector(".accordion-content");
      if (!header || !content) return;

      if (content.id && content.id === lastActiveId) {
        item.classList.add("open");
      }

      header.addEventListener("click", () => {
        const isOpen = item.classList.contains("open");
        accordionItems.forEach((otherItem) => {
          otherItem.classList.remove("open");
        });
        if (!isOpen) {
          item.classList.add("open");
          localStorage.setItem(ACCORDION_STORAGE_KEY, content.id);
        } else {
          localStorage.removeItem(ACCORDION_STORAGE_KEY);
        }
      });
    });

    if (!lastActiveId && accordionItems.length > 0) {
      accordionItems[0].classList.add("open");
    }

    // --- CHIAVI E LOGICA INPUT (CON MEMORIA) ---
    const YOUTUBE_URL_KEY = "magazzino_last_youtube_url";
    const RSS_URL_KEY = "magazzino_last_rss_url";
    const channelUrlInputYoutube = document.getElementById("channel_url");
    const rssUrlInput = document.getElementById("rss-url");
    if (channelUrlInputYoutube) {
      const savedYoutubeUrl = localStorage.getItem(YOUTUBE_URL_KEY);
      if (savedYoutubeUrl) {
        channelUrlInputYoutube.value = savedYoutubeUrl;
      }
    }
    if (rssUrlInput) {
      const savedRssUrl = localStorage.getItem(RSS_URL_KEY);
      if (savedRssUrl) {
        rssUrlInput.value = savedRssUrl;
      }
    }

    // --- LOGICA DRAG & DROP ---
    const dropArea = document.getElementById("file-drop-label");
    const fileInput = document.getElementById("file-input");
    if (dropArea && fileInput) {
      ["dragenter", "dragover", "dragleave", "drop"].forEach((eventName) => {
        dropArea.addEventListener(
          eventName,
          (e) => {
            e.preventDefault();
            e.stopPropagation();
          },
          false
        );
      });
      ["dragenter", "dragover"].forEach((eventName) => {
        dropArea.addEventListener(
          eventName,
          () => dropArea.classList.add("dragover"),
          false
        );
      });
      ["dragleave", "drop"].forEach((eventName) => {
        dropArea.addEventListener(
          eventName,
          () => dropArea.classList.remove("dragover"),
          false
        );
      });
      dropArea.addEventListener(
        "drop",
        (e) => {
          fileInput.files = e.dataTransfer.files;
          const event = new Event("change");
          fileInput.dispatchEvent(event);
        },
        false
      );
    }

    // --- SELEZIONE ALTRI ELEMENTI DOM ---
    const formYoutube = document.getElementById("youtube-form");
    const processBtnYoutube = document.getElementById("process-videos-btn");
    const scheduleYoutubeBtn = document.getElementById("schedule-youtube-btn");
    const uploadDocBtnDocs = document.getElementById("upload-doc-btn");
    const docUploadStatusDocs = document.getElementById("doc-upload-status");
    const selectedFileInfoDocs = document.getElementById("selected-file-info");
    const processRssBtn = document.getElementById("process-rss-btn");
    const scheduleRssBtn = document.getElementById("schedule-rss-btn");
    const statusDivAsync = document.getElementById("process-status");
    const statusMessageSpanAsync = document.getElementById("status-message");
    const progressBarContainerAsync = document.getElementById(
      "progress-bar-container"
    );
    const progressBarAsync = document.getElementById("progress-bar");

    // --- VARIABILI DI STATO ---
    let progressInterval = null;
    let timerInterval = null;
    let secondsElapsed = 0;
    let currentPollingType = null;
    let lastProcessedYoutubeUrl = null;
    let lastProcessedRssUrl = null;

    // --- FUNZIONI HELPER UI ---
    function disableAllForms() {
      if (processBtnYoutube) processBtnYoutube.disabled = true;
      if (channelUrlInputYoutube) channelUrlInputYoutube.disabled = true;
      if (uploadDocBtnDocs) uploadDocBtnDocs.disabled = true;
      if (fileInput) fileInput.disabled = true;
      if (processRssBtn) processRssBtn.disabled = true;
      if (rssUrlInput) rssUrlInput.disabled = true;
    }
    function enableAllForms() {
      if (processBtnYoutube) processBtnYoutube.disabled = false;
      if (channelUrlInputYoutube) channelUrlInputYoutube.disabled = false;
      if (uploadDocBtnDocs && fileInput.files.length === 0) {
        uploadDocBtnDocs.disabled = true;
      } else if (uploadDocBtnDocs) {
        uploadDocBtnDocs.disabled = false;
      }
      if (fileInput) fileInput.disabled = false;
      if (processRssBtn) processRssBtn.disabled = false;
      if (rssUrlInput) rssUrlInput.disabled = false;
    }
    function resetAsyncStatusUI() {
      if (statusDivAsync) statusDivAsync.style.display = "none";
      if (scheduleYoutubeBtn) scheduleYoutubeBtn.style.display = "none";
      if (scheduleRssBtn) scheduleRssBtn.style.display = "none";
    }
    
    function updateAsyncStatusUI(message, statusClass, progressPercent = null, isIndeterminate = false) {
            if (!statusDivAsync || !statusMessageSpanAsync) return;

            statusDivAsync.style.display = "block";
            statusDivAsync.className = statusClass;
            
            statusMessageSpanAsync.innerHTML = message;

            if (progressBarContainerAsync && progressBarAsync) {
                progressBarContainerAsync.style.display = "block";
                
                if (isIndeterminate) {
                    // Se è indeterminato, mettiamo la barra al 100% e aggiungiamo l'animazione
                    progressBarAsync.style.width = `100%`;
                    progressBarAsync.classList.add('progress-bar-indeterminate');
                } else {
                    // Altrimenti, comportamento normale
                    progressBarAsync.classList.remove('progress-bar-indeterminate');
                    if (progressPercent !== null) {
                        progressBarAsync.style.width = `${progressPercent}%`;
                    } else {
                        progressBarContainerAsync.style.display = "none";
                    }
                }
            }
        }

    // --- FUNZIONI CRONOMETRO ---
    function startTimer() {
      stopTimer();
      secondsElapsed = 0;
      const existingTimer = document.getElementById("async-timer");
      if (existingTimer) existingTimer.remove();
      const timerElement = document.createElement("span");
      timerElement.id = "async-timer";
      timerElement.style.marginLeft = "15px";
      timerElement.style.fontStyle = "italic";
      timerElement.style.color = "var(--color-text-secondary)";
      if (statusDivAsync) statusDivAsync.appendChild(timerElement);
      timerInterval = setInterval(() => {
        secondsElapsed++;
        timerElement.textContent = `(${secondsElapsed}s)`;
      }, 1000);
    }
    function stopTimer() {
      if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
      }
    }

    // --- GESTIONE YOUTUBE ---
    if (formYoutube) {
      formYoutube.addEventListener("submit", async function (event) {
        event.preventDefault();
        const channelUrl = channelUrlInputYoutube.value.trim();
        if (!channelUrl) return;
        localStorage.setItem(YOUTUBE_URL_KEY, channelUrl);
        if (window.setAppStatus) window.setAppStatus("processing");
        startTimer();
        lastProcessedYoutubeUrl = channelUrl;
        disableAllForms();
        resetAsyncStatusUI();
        updateAsyncStatusUI(
          'Avvio elaborazione canale... <span class="loading-spinner"></span>',
          "info-message"
        );
        processBtnYoutube.textContent = "Elaborazione...";
        try {
          const response = await fetch("/api/videos/channel", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
            },
            body: JSON.stringify({ channel_url: channelUrl }),
          });
          const data = await response.json();
          if (response.status === 202 && data.success) {
            startProgressPolling("youtube");
          } else {
            throw new Error(data.message || `Errore ${response.status}`);
          }
        } catch (error) {
          let errorMessage = `Errore: ${error.message}`;
          if (error instanceof TypeError) {
            errorMessage =
              "Il server ha impiegato troppo tempo a rispondere. Riprova tra poco.";
          }
          updateAsyncStatusUI(errorMessage, "error-message");
          enableAllForms();
          processBtnYoutube.textContent = "Processa Video";
          if (window.setAppStatus) window.setAppStatus("idle");
          stopTimer();
        }
      });
    }

    // --- GESTIONE RSS ---
    if (processRssBtn) {
      processRssBtn.addEventListener("click", async () => {
        const feedUrl = rssUrlInput.value.trim();
        if (!feedUrl) return;
        localStorage.setItem(RSS_URL_KEY, feedUrl);
        if (window.setAppStatus) window.setAppStatus("processing");
        startTimer();
        lastProcessedRssUrl = feedUrl;
        disableAllForms();
        resetAsyncStatusUI();
        updateAsyncStatusUI(
          'Avvio elaborazione RSS... <span class="loading-spinner"></span>',
          "info-message"
        );
        processRssBtn.textContent = "Processo...";
        try {
          const response = await fetch("/api/rss/process", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rss_url: feedUrl }),
          });
          const data = await response.json();
          if (response.status === 202) {
            startProgressPolling("rss");
          } else {
            throw new Error(data.message);
          }
        } catch (error) {
          let errorMessage = `Errore: ${error.message}`;
          if (error instanceof TypeError) {
            errorMessage =
              "Il server ha impiegato troppo tempo a rispondere. Riprova tra poco.";
          }
          updateAsyncStatusUI(errorMessage, "error-message");
          enableAllForms();
          processRssBtn.textContent = "Processa Feed";
          if (window.setAppStatus) window.setAppStatus("idle");
          stopTimer();
        }
      });
    }

    // --- FUNZIONE DI POLLING (VERSIONE CORRETTA) ---
    function startProgressPolling(type) {
      clearInterval(progressInterval);
      currentPollingType = type;
      progressInterval = setInterval(() => checkProgress(type), 1500); // Leggermente più veloce
    }
    
    async function checkProgress(type) {
        const endpoint = type === "youtube" ? "/api/videos/progress" : "/api/rss/progress";
        try {
            const response = await fetch(endpoint);
            const progressData = await response.json();

            if (progressData.is_processing) {
                // --- NUOVA LOGICA ---
                // Se il backend ci dice che è un passo indeterminato, mostriamo la barra animata.
                if (progressData.indeterminate_step) {
                    updateAsyncStatusUI(
                        `${progressData.message} <span class="loading-spinner"></span>`, 
                        "info-message", 
                        null, // La percentuale non serve
                        true  // Attiva la barra animata!
                    );
                } else {
                    // Altrimenti, procediamo con la logica normale della percentuale.
                    let msg = progressData.message || "Elaborazione in corso...";
                    let perc = null;
                    if (type === "youtube" && progressData.current_video && progressData.current_video.total > 0) {
                        const current = progressData.current_video.index || 0;
                        const total = progressData.current_video.total;
                        perc = Math.round((current / total) * 100);
                        // Usiamo il messaggio del backend se disponibile, altrimenti creiamo il nostro
                        msg = progressData.message || `(${current}/${total}) ${progressData.current_video.title || "video..."}`;
                    } else if (type === "rss" && progressData.page_total_articles && progressData.page_total_articles > 0) {
                        const current = progressData.page_processed_articles || 0;
                        const total = progressData.page_total_articles;
                        perc = Math.round((current / total) * 100);
                        msg = progressData.message;
                    }
                    updateAsyncStatusUI(
                        `${msg} <span class="loading-spinner"></span>`, 
                        "info-message", // Usiamo 'info' per coerenza
                        perc
                    );
                }
            } else {
                // Logica di fine processo (invariata)
                clearInterval(progressInterval);
                stopTimer();
                if (window.setAppStatus) window.setAppStatus("idle");
                const finalMessage = progressData.message || "Elaborazione completata.";
                const finalStatusClass = progressData.error ? "error-message" : "success-message";
                const finalPerc = !progressData.error ? 100 : null;
                updateAsyncStatusUI(progressData.error || finalMessage, finalStatusClass, finalPerc);
                if (finalStatusClass === "success-message") {
                    setTimeout(() => { window.location.reload(); }, 1500);
                } else {
                    enableAllForms();
                }
            }
        } catch (error) {
            clearInterval(progressInterval);
            stopTimer();
            if (window.setAppStatus) window.setAppStatus("idle");
            updateAsyncStatusUI("Errore nel controllo dello stato.", "error-message");
            enableAllForms();
            if (type === "youtube") processBtnYoutube.textContent = "Processa Video";
            if (type === "rss") processRssBtn.textContent = "Processa Feed";
            currentPollingType = null;
        }
    }


    // --- GESTIONE DOCUMENTI ---
    if (uploadDocBtnDocs) {
      fileInput.addEventListener("change", () => {
        uploadDocBtnDocs.disabled = fileInput.files.length === 0;
        selectedFileInfoDocs.textContent = fileInput.files.length > 0 ? `${fileInput.files.length} file selezionati.` : "";
      });
      uploadDocBtnDocs.addEventListener("click", async () => {
        const formData = new FormData();
        for (const file of fileInput.files) {
          formData.append("documents", file);
        }
        docUploadStatusDocs.innerHTML = `Caricamento... <span class="loading-spinner"></span>`;
        docUploadStatusDocs.className = "info-message";
        docUploadStatusDocs.style.display = "block";
        uploadDocBtnDocs.disabled = true;
        
        try {
          const response = await fetch("/api/documents/upload", {
            method: "POST",
            body: formData,
          });
          const data = await response.json();
          
          if (response.ok && data.success) {
            docUploadStatusDocs.textContent = data.message + " La pagina si ricaricherà a breve.";
            docUploadStatusDocs.className = "success-message";
            setTimeout(() => {
                window.location.reload();
            }, 2000);
          } else {
            throw new Error(data.message || 'Errore durante l\'upload.');
          }
        } catch (error) {
          docUploadStatusDocs.textContent = `Errore: ${error.message}`;
          docUploadStatusDocs.className = "error-message";
          uploadDocBtnDocs.disabled = false;
        }
      });
    }

    // --- GESTIONE LINK "PIANIFICA" ---
    if (scheduleYoutubeBtn) {
      scheduleYoutubeBtn.addEventListener("click", function (e) {
        e.preventDefault();
        const url = e.target.getAttribute("data-url");
        if (url)
          window.location.href = `/automations?type=youtube&url=${encodeURIComponent(
            url
          )}`;
      });
    }
    if (scheduleRssBtn) {
      scheduleRssBtn.addEventListener("click", function (e) {
        e.preventDefault();
        const url = e.target.getAttribute("data-url");
        if (url)
          window.location.href = `/automations?type=rss&url=${encodeURIComponent(
            url
          )}`;
      });
    }
  });