from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
import logging
from typing import Optional, Dict
from app.services.youtube.client import YouTubeClient
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

class TranscriptService:  
    @staticmethod
    def get_transcript(video_id: str, youtube_client: YouTubeClient, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        """
        Wrapper per recuperare una trascrizione usando il YouTubeClient autenticato.
        Questo metodo ora richiede un'istanza di YouTubeClient.
        """
        if not youtube_client:
            logger.error(f"[{video_id}] Tentativo di chiamare get_transcript senza un YouTubeClient valido.")
            return {'error': 'CLIENT_NOT_PROVIDED', 'message': 'YouTubeClient non fornito.'}

        # La logica è ora delegata interamente al metodo del client
        return youtube_client.get_transcript_by_api(video_id, preferred_languages)