from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
import logging
from typing import Optional, Dict
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

class TranscriptService:
    @staticmethod
    def _fetch_and_join_transcript(transcript_object) -> Optional[str]:
        """Helper interno per fetchare e unire la trascrizione, gestendo errori di parsing."""
        try:
            fetched_transcript = transcript_object.fetch()
            if fetched_transcript:
                text = ' '.join([t['text'] for t in fetched_transcript if 'text' in t])
                return text
            else:
                logger.warning(f"Fetch della trascrizione per {transcript_object.video_id} in lingua {transcript_object.language} ha restituito None o vuoto.")
                return None
        except ET.ParseError as e_xml:
            logger.error(f"Errore parsing XML durante fetch trascrizione per video {transcript_object.video_id}, lingua {transcript_object.language}: {e_xml}")
            return None
        except KeyError as e_key: 
            logger.error(f"Chiave 'text' mancante in un segmento di trascrizione per video {transcript_object.video_id}, lingua {transcript_object.language}: {e_key}")
            return None
        except Exception as e_fetch: 
            logger.error(f"Errore imprevisto durante fetch/join trascrizione per video {transcript_object.video_id}, lingua {transcript_object.language}: {e_fetch}", exc_info=False)
            raise e_fetch 

    @staticmethod
    def get_transcript(video_id: str, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        logger.info(f"Inizio recupero trascrizione per video ID: {video_id}")
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

            # Cerca manuali nelle lingue preferite
            for lang in preferred_languages:
                try:
                    transcript_obj = transcript_list.find_manually_created_transcript([lang])
                    text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                    if text:
                        logger.info(f"[{video_id}] Testo manuale '{lang}' recuperato (lunghezza: {len(text)}).")
                        return {'text': text, 'language': lang, 'type': 'manual'}
                except NoTranscriptFound:
                    continue # Prova la lingua successiva

            # Cerca automatiche nelle lingue preferite
            for lang in preferred_languages:
                try:
                    transcript_obj = transcript_list.find_generated_transcript([lang])
                    text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                    if text:
                        logger.info(f"[{video_id}] Testo automatico '{lang}' recuperato (lunghezza: {len(text)}).")
                        return {'text': text, 'language': lang, 'type': 'auto'}
                except NoTranscriptFound:
                    continue # Prova la lingua successiva
            
            # Fallback: qualsiasi manuale
            try:
                transcript_obj = transcript_list.find_manually_created_transcript([])
                lang_found = transcript_obj.language
                text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                if text:
                    logger.info(f"[{video_id}] Testo manuale fallback '{lang_found}' recuperato (lunghezza: {len(text)}).")
                    return {'text': text, 'language': lang_found, 'type': 'manual'}
            except NoTranscriptFound:
                pass # Nessun manuale di fallback, procedi

            # Fallback: qualsiasi automatica
            try:
                transcript_obj = transcript_list.find_generated_transcript([])
                lang_found = transcript_obj.language
                text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                if text:
                    logger.info(f"[{video_id}] Testo automatico fallback '{lang_found}' recuperato (lunghezza: {len(text)}).")
                    return {'text': text, 'language': lang_found, 'type': 'auto'}
            except NoTranscriptFound:
                logger.warning(f"[{video_id}] NESSUNA TRASCRIZIONE (manuale o auto, fallback) trovata in NESSUNA lingua.")
                return {'error': 'NO_TRANSCRIPT_FOUND', 'message': 'Nessuna trascrizione trovata per questo video in nessuna lingua.'}

        except TranscriptsDisabled:
            logger.warning(f"[{video_id}] ERRORE: Le trascrizioni sono disabilitate per questo video.")
            return {'error': 'TRANSCRIPTS_DISABLED', 'message': 'Le trascrizioni sono disabilitate per questo video.'}
        except Exception as e: 
            error_message = str(e)
            if '429' in error_message and 'Too Many Requests' in error_message:
                logger.error(f"[{video_id}] ERRORE 429: Richiesta bloccata da YouTube.")
                return {'error': 'YOUTUBE_BLOCKED', 'message': 'YouTube ha bloccato la richiesta (Errore 429). Questo accade spesso quando l\'applicazione è su un server cloud.'}
            
            logger.error(f"[{video_id}] ERRORE IMPREVISTO durante il recupero della trascrizione: {error_message}", exc_info=True)
            return {'error': 'UNKNOWN_ERROR', 'message': f'Errore imprevisto durante il recupero: {error_message[:200]}...'}

        logger.warning(f"[{video_id}] Nessuna trascrizione valida trovata dopo tutti i tentativi.")
        return {'error': 'NO_TRANSCRIPT_FOUND', 'message': 'Nessuna trascrizione valida trovata dopo tutti i tentativi.'}