from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from typing import List, Optional, Union, Dict
from googleapiclient.errors import HttpError
import logging
import re
import time
import os

from app.api.models.video import Video

logger = logging.getLogger(__name__)

class YouTubeClient:
    def __init__(self, token_file: str = "token.json"):
        self.token_file = token_file
        self.youtube = None
        self._init_service()

    def _init_service(self):
        """Inizializza il servizio YouTube con le credenziali"""
        try:
            if not os.path.exists(self.token_file):
                raise ValueError("Credentials not found. Please authenticate first.")
                
            credentials = Credentials.from_authorized_user_file(self.token_file)
            self.youtube = build('youtube', 'v3', credentials=credentials)
        except Exception as e:
            logger.error(f"Error initializing YouTube service: {str(e)}")
            raise

    def extract_channel_info(self, url_or_id: str) -> str:
        """Estrae l'ID del canale da vari formati di URL"""
        try:
            # Se è già un ID del canale
            if url_or_id.startswith('UC'):
                return url_or_id

            # Estrai l'handle o l'ID dal URL
            patterns = [
                r'youtube\.com/channel/(UC[\w-]+)',  # Pattern per ID canale
                r'youtube\.com/@([\w-]+)',           # Pattern per handle
                r'youtube\.com/user/([\w-]+)',       # Pattern per username legacy
                r'youtube\.com/c/([\w-]+)',          # Pattern per nome personalizzato
                r'@([\w-]+)'                         # Pattern per handle diretto
            ]

            for pattern in patterns:
                match = re.search(pattern, url_or_id)
                if match:
                    identifier = match.group(1)
                    if identifier.startswith('UC'):
                        return identifier
                    else:
                        # Per gli handle moderni (@username), usa la ricerca
                        try:
                            request = self.youtube.search().list(
                                part='snippet',
                                q=identifier,
                                type='channel',
                                maxResults=1
                            )
                            response = request.execute()
                            if response['items']:
                                return response['items'][0]['id']['channelId']
                        except Exception as e:
                            logger.warning(f"Error finding channel by handle: {str(e)}")
                            
                        # Prova con il metodo legacy per username
                        try:
                            request = self.youtube.channels().list(
                                part='id',
                                forUsername=identifier
                            )
                            response = request.execute()
                            if response['items']:
                                return response['items'][0]['id']
                        except Exception as e:
                            logger.warning(f"Error finding channel by username: {str(e)}")
                            
            raise ValueError("Unable to find channel ID")
        except Exception as e:
            logger.error(f"Error extracting channel info: {str(e)}")
            raise

    def get_channel_videos(self, channel_id: str, limit: Optional[int] = None) -> List[Video]:
        """
        Recupera i video di un canale, gestendo la paginazione.

        Args:
            channel_id: L'ID del canale YouTube.
            limit: Numero massimo di video da recuperare. Se None, prova a recuperarli tutti.

        Returns:
            Una lista di oggetti Video.
        """
        all_videos = []
        next_page_token = None
        max_results_per_page = 50 # Limite massimo per richiesta search.list

        logger.info(f"Inizio recupero video per canale {channel_id}. Limite: {'Tutti' if limit is None else limit}")

        try:
            while True: # Continua finché ci sono pagine o non raggiungiamo il limite
                logger.debug(f"Recupero pagina video... Token: {next_page_token}")
                request = self.youtube.search().list(
                    part='id,snippet',
                    channelId=channel_id,
                    maxResults=max_results_per_page,
                    type='video',
                    order='date',
                    pageToken=next_page_token # Passa il token per la pagina successiva
                )

                response = request.execute()

                for item in response.get('items', []):
                    # Controlla se l'item è effettivamente un video (a volte search può restituire altro)
                    if item.get('id', {}).get('kind') == 'youtube#video':
                        video = Video(
                            video_id=item['id']['videoId'],
                            title=item['snippet']['title'],
                            url=f"https://www.youtube.com/watch?v={item['id']['videoId']}",
                            channel_id=channel_id, # Usiamo l'ID passato, snippet può avere channelTitle
                            published_at=item['snippet']['publishedAt'],
                            description=item['snippet']['description']
                        )
                        all_videos.append(video)
                        # Controlla se abbiamo raggiunto il limite impostato
                        if limit is not None and len(all_videos) >= limit:
                            logger.info(f"Raggiunto limite di {limit} video.")
                            return all_videos # Interrompi e restituisci

                # Passa alla pagina successiva
                next_page_token = response.get('nextPageToken')
                logger.info(f"Recuperati finora: {len(all_videos)} video. Prossima pagina token: {'Sì' if next_page_token else 'No'}")

                # Interrompi il loop se non c'è più un nextPageToken
                if not next_page_token:
                    logger.info("Nessun altra pagina di risultati.")
                    break

            logger.info(f"Recupero video completato. Totale video trovati: {len(all_videos)}")
            return all_videos

        except Exception as e:
            logger.exception(f"Errore durante il recupero dei video del canale {channel_id}: {str(e)}")
            # Restituisce i video recuperati finora, anche se c'è stato un errore
            # Potrebbe essere preferibile rilanciare l'eccezione in alcuni casi
            logger.warning(f"Restituisco {len(all_videos)} video recuperati prima dell'errore.")
            return all_videos

    def get_video_details(self, video_id: str) -> Video:
        """Recupera i dettagli di un singolo video"""
        try:
            request = self.youtube.videos().list(
                part='snippet,contentDetails,statistics',
                id=video_id
            )
            response = request.execute()

            if not response['items']:
                raise ValueError(f"No details found for video ID {video_id}")

            item = response['items'][0]
            video = Video(
                video_id=video_id,
                title=item['snippet']['title'],
                url=f"https://www.youtube.com/watch?v={video_id}",
                channel_id=item['snippet']['channelId'],
                published_at=item['snippet']['publishedAt'],
                description=item['snippet']['description']
            )
            return video

        except Exception as e:
            logger.error(f"Error getting video details for {video_id}: {str(e)}")
            raise 

    def get_transcript_by_api(self, video_id: str, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        """
        Recupera una trascrizione usando l'API ufficiale di YouTube.
        Cerca nelle lingue preferite e gestisce l'errore di quota con dei tentativi.
        """
        logger.info(f"[API Ufficiale] Avvio recupero trascrizione per video ID: {video_id}")
        
        # NUOVO BLOCCO: Logica di tentativi e attesa
        retries = 3
        delay = 15  # Inizia con 15 secondi di attesa

        for attempt in range(retries):
            try:
                # 1. Ottieni la lista delle tracce di sottotitoli disponibili per il video
                list_request = self.youtube.captions().list(
                    part='snippet',
                    videoId=video_id
                )
                list_response = list_request.execute()

                available_tracks = {}
                for item in list_response.get('items', []):
                    lang = item['snippet']['language']
                    track_id = item['id']
                    track_type = 'manual' if item['snippet']['trackKind'] == 'standard' else 'auto'
                    available_tracks[lang] = {'id': track_id, 'type': track_type}
                
                if not available_tracks:
                    logger.warning(f"[API Ufficiale] Nessuna traccia di sottotitoli trovata per {video_id}.")
                    return {'error': 'NO_TRANSCRIPT_FOUND', 'message': 'Nessuna traccia (manuale o auto) disponibile.'}

                # 2. Cerca una traccia nelle lingue preferite
                track_to_download = None
                found_lang = None
                for lang_code in preferred_languages:
                    if lang_code in available_tracks:
                        track_to_download = available_tracks[lang_code]
                        found_lang = lang_code
                        logger.info(f"[API Ufficiale] Trovata traccia preferita '{found_lang}' (Tipo: {track_to_download['type']}).")
                        break
                
                if not track_to_download:
                    first_lang = next(iter(available_tracks))
                    track_to_download = available_tracks[first_lang]
                    found_lang = first_lang
                    logger.info(f"[API Ufficiale] Nessuna lingua preferita. Uso fallback: '{found_lang}' (Tipo: {track_to_download['type']}).")

                # 3. Scarica la traccia selezionata
                download_request = self.youtube.captions().download(
                    id=track_to_download['id'],
                    tfmt='srt'
                )
                srt_captions = download_request.execute().decode('utf-8')
                
                # 4. Pulisci il testo SRT
                text_no_seq = re.sub(r'^\d+\s*$', '', srt_captions, flags=re.MULTILINE)
                text_no_ts = re.sub(r'\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\s*$', '', text_no_seq, flags=re.MULTILINE)
                text_no_html = re.sub(r'<[^>]+>', '', text_no_ts)
                clean_text = ' '.join(text_no_html.strip().split())

                logger.info(f"[API Ufficiale] Trascrizione scaricata e pulita per {video_id} (lingua: {found_lang}).")
                return {
                    'text': clean_text,
                    'language': found_lang,
                    'type': track_to_download['type']
                }

            except HttpError as e:
                # NUOVO BLOCCO: Gestione specifica dell'errore di quota
                if e.status_code == 403 and 'quotaExceeded' in str(e):
                    logger.warning(f"[API Ufficiale] Quota YouTube superata (tentativo {attempt + 1}/{retries}). Attendo {delay} secondi...")
                    if attempt < retries - 1: # Se non è l'ultimo tentativo
                        time.sleep(delay)
                        delay *= 2 # Raddoppia l'attesa per il prossimo tentativo
                        continue # Passa al prossimo ciclo del for
                    else:
                        logger.error(f"[API Ufficiale] Quota superata anche dopo {retries} tentativi. Mi arrendo per questo video.")
                        return {'error': 'QUOTA_EXCEEDED', 'message': f'Quota API di YouTube superata.'}
                
                # Altri errori HttpError (es. trascrizioni disabilitate) vengono gestiti come prima
                error_str = str(e).lower()
                if 'disabled' in error_str or 'forbidden' in error_str:
                    logger.warning(f"[API Ufficiale] Le trascrizioni sono disabilitate per {video_id}.")
                    return {'error': 'TRANSCRIPTS_DISABLED', 'message': 'Le trascrizioni sono disabilitate per questo video.'}
                
                # Se è un HttpError non gestito, lo rilanciamo per farlo vedere nei log
                raise e
            
            except Exception as e:
                logger.error(f"[API Ufficiale] Errore imprevisto durante il recupero trascrizione per {video_id}: {e}", exc_info=True)
                return {'error': 'UNKNOWN_API_ERROR', 'message': f'Errore API YouTube: {str(e)[:150]}...'}

        # Se il ciclo finisce senza successo, restituisce un errore generico
        return {'error': 'MAX_RETRIES_REACHED', 'message': 'Raggiunto numero massimo di tentativi senza successo.'}