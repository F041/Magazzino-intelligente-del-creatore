from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from typing import List, Optional, Union, Dict, Tuple
from googleapiclient.errors import HttpError
import logging
import re
import time
import os
import html

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
            if url_or_id.startswith('UC'):
                return url_or_id

            patterns = [
                r'youtube\.com/channel/(UC[\w-]+)',
                r'youtube\.com/@([\w-]+)',
                r'youtube\.com/user/([\w-]+)',
                r'youtube\.com/c/([\w-]+)',
                r'@([\w-]+)'
            ]

            for pattern in patterns:
                match = re.search(pattern, url_or_id)
                if match:
                    identifier = match.group(1)
                    if identifier.startswith('UC'):
                        return identifier
                    else:
                        try:
                            request = self.youtube.search().list(
                                part='snippet', q=identifier, type='channel', maxResults=1
                            )
                            response = request.execute()
                            if response['items']:
                                return response['items'][0]['id']['channelId']
                        except Exception as e:
                            logger.warning(f"Error finding channel by handle: {str(e)}")
                            
                        try:
                            request = self.youtube.channels().list(
                                part='id', forUsername=identifier
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

    def get_channel_videos_and_total_count(self, channel_id: str) -> Tuple[List[Video], int]:
        """
        Recupera TUTTI i video di un canale, gestendo la paginazione e gli errori di quota.
        """
        all_videos = []
        next_page_token = None
        total_results = 0

        logger.info(f"Inizio recupero video per canale {channel_id}.")

        try:
            while True:
                logger.debug(f"Recupero pagina video... Token: {next_page_token}")
                request = self.youtube.search().list(
                    part='id,snippet',
                    channelId=channel_id,
                    maxResults=50,
                    type='video',
                    order='date',
                    pageToken=next_page_token
                )
                response = request.execute()

                if next_page_token is None:
                    total_results = response.get('pageInfo', {}).get('totalResults', 0)
                    logger.info(f"Conteggio totale video dal canale: {total_results}")

                for item in response.get('items', []):
                    if item.get('id', {}).get('kind') == 'youtube#video':
                        video = Video(
                            video_id=item['id']['videoId'],
                            title=html.unescape(item['snippet']['title']),
                            url=f"https://www.youtube.com/watch?v={item['id']['videoId']}",
                            channel_id=channel_id,
                            published_at=item['snippet']['publishedAt'],
                            description=html.unescape(item['snippet']['description'])
                        )
                        all_videos.append(video)

                next_page_token = response.get('nextPageToken')
                if not next_page_token:
                    break
            
            logger.info(f"Recupero video completato. Trovati: {len(all_videos)} video.")
            return all_videos, total_results

        except HttpError as e:
            # --- INIZIO BLOCCO DI GESTIONE ERRORE ---
            # Controlliamo specificamente se l'errore è un 403 per quota esaurita.
            if e.resp.status == 403 and 'quotaExceeded' in str(e):
                logger.error(f"QUOTA API YOUTUBE SUPERATA durante il recupero della lista video per il canale {channel_id}.")
                # Rilanciamo l'eccezione, così il chiamante sa esattamente cosa è successo
                # e non riceve una lista vuota.
                raise e
            else:
                # Se è un altro errore HTTP, lo logghiamo e lo rilanciamo.
                logger.exception(f"Errore HTTP durante il recupero dei video del canale {channel_id}: {str(e)}")
                raise e
            # --- FINE BLOCCO DI GESTIONE ERRORE ---
        
        except Exception as e:
            logger.exception(f"Errore generico durante il recupero dei video del canale {channel_id}: {str(e)}")
            # Anche in caso di altri errori, è meglio rilanciare l'eccezione
            raise e


    def get_video_details(self, video_id: str) -> Video:
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
                title=html.unescape(item['snippet']['title']),
                url=f"https://www.youtube.com/watch?v={video_id}",
                channel_id=item['snippet']['channelId'],
                published_at=item['snippet']['publishedAt'],
                description=html.unescape(item['snippet']['description'])
            )
            return video

        except Exception as e:
            logger.error(f"Error getting video details for {video_id}: {str(e)}")
            raise 

    def get_transcript_by_api(self, video_id: str, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        logger.info(f"[API Ufficiale] Avvio recupero trascrizione per video ID: {video_id}")
        
        retries = 3
        delay = 15

        for attempt in range(retries):
            try:
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
                    return {'error': 'NO_TRANSCRIPT_FOUND', 'message': 'Nessuna traccia (manuale o auto) disponibile.'}

                track_to_download = None
                found_lang = None
                for lang_code in preferred_languages:
                    if lang_code in available_tracks:
                        track_to_download = available_tracks[lang_code]
                        found_lang = lang_code
                        break
                
                if not track_to_download:
                    first_lang = next(iter(available_tracks))
                    track_to_download = available_tracks[first_lang]
                    found_lang = first_lang

                download_request = self.youtube.captions().download(
                    id=track_to_download['id'],
                    tfmt='srt'
                )
                srt_captions = download_request.execute().decode('utf-8')
                
                text_no_seq = re.sub(r'^\d+\s*$', '', srt_captions, flags=re.MULTILINE)
                text_no_ts = re.sub(r'\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\s*$', '', text_no_seq, flags=re.MULTILINE)
                text_no_html = re.sub(r'<[^>]+>', '', text_no_ts)
                clean_text = ' '.join(text_no_html.strip().split())

                return {
                    'text': clean_text,
                    'language': found_lang,
                    'type': track_to_download['type']
                }

            except HttpError as e:
                if e.resp.status == 403 and 'quotaExceeded' in str(e):
                    if attempt < retries - 1:
                        time.sleep(delay)
                        delay *= 2 
                        continue
                    else:
                        return {'error': 'QUOTA_EXCEEDED', 'message': f'Quota API di YouTube superata.'}
                
                error_str = str(e).lower()
                if 'disabled' in error_str or 'forbidden' in error_str:
                    return {'error': 'TRANSCRIPTS_DISABLED', 'message': 'Le trascrizioni sono disabilitate per questo video.'}
                
                raise e
            
            except Exception as e:
                return {'error': 'UNKNOWN_API_ERROR', 'message': f'Errore API YouTube: {str(e)[:150]}...'}

        return {'error': 'MAX_RETRIES_REACHED', 'message': 'Raggiunto numero massimo di tentativi senza successo.'}