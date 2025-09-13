import requests
import logging
from typing import List, Dict, Optional

# Impostiamo un logger specifico per questo client, così i log saranno chiari
logger = logging.getLogger(__name__)

class WordPressClient:
    """
    Un client per comunicare con l'API REST di WordPress,
    recuperare articoli (posts) e pagine (pages).
    """
    def __init__(self, site_url: str, username: str, app_password: str):
        """
        Inizializza il client con le credenziali necessarie.
        
        Args:
            site_url (str): L'URL base del sito WordPress (es. "https://www.ilmiosito.com").
            username (str): Il nome utente dell'amministratore a cui è associata la password.
            app_password (str): La Application Password generata da WordPress.
        """
        if not site_url:
            raise ValueError("L'URL del sito WordPress è obbligatorio.")
        
        # Pulisce l'URL per assicurarsi che sia nel formato corretto
        self.base_url = site_url.rstrip('/') + "/wp-json/wp/v2"
        self.auth = (username, app_password)
        self.headers = {'User-Agent': 'MagazzinoDelCreatore/1.0'}

        logger.info(f"Client WordPress inizializzato per il sito: {site_url}")

    def _get_all_paginated_results(self, endpoint: str) -> List[Dict]:
        """
        Funzione helper per recuperare TUTTI i risultati da un endpoint che usa la paginazione.
        WordPress ci dice quante pagine ci sono in totale negli header della risposta.
        """
        all_items = []
        page = 1
        total_pages = 1 # Partiamo assumendo che ci sia almeno una pagina

        while page <= total_pages:
            # Aggiungiamo i parametri alla richiesta:
            # - page: il numero di pagina che vogliamo
            # - per_page: quanti risultati per pagina (100 è il massimo per WordPress)
            # - _fields: chiediamo a WordPress di inviarci SOLO i campi che ci servono.
            #            Questo rende la richiesta molto più veloce e leggera.
            params = {
                'page': page,
                'per_page': 100,
                '_fields': 'id,link,title,content,modified_gmt,type'
            }
            
            logger.info(f"Recupero dati dall'endpoint '{endpoint}', pagina {page}/{total_pages}...")
            
            try:
                response = requests.get(f"{self.base_url}/{endpoint}", headers=self.headers, auth=self.auth, params=params, timeout=30)
                # Questo solleverà un errore se la risposta è 4xx o 5xx (es. credenziali sbagliate)
                response.raise_for_status() 

                # La prima volta, leggiamo il numero totale di pagine dall'header
                if page == 1:
                    total_pages = int(response.headers.get('X-WP-TotalPages', 1))
                    logger.info(f"Trovate {total_pages} pagine totali per '{endpoint}'.")

                data = response.json()
                if not data: # Se una pagina è vuota, abbiamo finito
                    break
                
                all_items.extend(data)
                page += 1

            except requests.exceptions.RequestException as e:
                logger.error(f"Errore di rete o HTTP durante la chiamata a {endpoint} (pagina {page}): {e}")
                # Interrompiamo il ciclo in caso di errore di rete
                break
            except Exception as e:
                logger.error(f"Errore imprevisto durante il recupero dei dati da {endpoint}: {e}")
                break

        logger.info(f"Recuperati {len(all_items)} item totali dall'endpoint '{endpoint}'.")
        return all_items

    def get_all_posts(self) -> List[Dict]:
        """Recupera tutti gli articoli (post) dal sito."""
        return self._get_all_paginated_results("posts")

    def get_all_pages(self) -> List[Dict]:
        """Recupera tutte le pagine (page) dal sito."""
        return self._get_all_paginated_results("pages")