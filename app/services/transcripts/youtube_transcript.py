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
            logger.error(f"Errore imprevisto durante fetch/join trascrizione per video {transcript_object.video_id}, lingua {transcript_object.language}: {e_fetch}", exc_info=False) # Messo exc_info=False per non appesantire troppo i log con traceback ripetuti per questo helper
            return None

    @staticmethod
    def get_transcript(video_id: str, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        logger.info(f"Inizio recupero trascrizione per video ID: {video_id}")
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            # logger.info(f"[{video_id}] Lista trascrizioni trovata.") # Meno verboso

            # Cerca manuali nelle lingue preferite
            for lang in preferred_languages:
                # logger.debug(f"[{video_id}] Tentativo ricerca manuale per lingua: {lang}")
                try:
                    transcript_obj = transcript_list.find_manually_created_transcript([lang])
                    # logger.info(f"[{video_id}] Trovata trascrizione MANUALE in '{lang}'. Recupero testo...")
                    text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                    if text:
                        logger.info(f"[{video_id}] Testo manuale '{lang}' recuperato (lunghezza: {len(text)}).")
                        return {'text': text, 'language': lang, 'type': 'manual'}
                    # else: logger.warning(f"[{video_id}] Fetch testo manuale '{lang}' fallito o testo vuoto.") # Gestito da _fetch_and_join
                except NoTranscriptFound:
                    pass # logger.debug(f"[{video_id}] Nessuna trascrizione manuale trovata per '{lang}'.")
                except Exception as e_find_manual: # Cattura altri errori durante la ricerca
                    logger.warning(f"[{video_id}] Errore durante find_manually_created_transcript per '{lang}': {e_find_manual}")


            # Cerca automatiche nelle lingue preferite
            # logger.info(f"[{video_id}] Provo trascrizioni automatiche nelle lingue preferite...")
            for lang in preferred_languages:
                # logger.debug(f"[{video_id}] Tentativo ricerca automatica per lingua: {lang}")
                try:
                    transcript_obj = transcript_list.find_generated_transcript([lang])
                    # logger.info(f"[{video_id}] Trovata trascrizione AUTOMATICA in '{lang}'. Recupero testo...")
                    text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                    if text:
                        logger.info(f"[{video_id}] Testo automatico '{lang}' recuperato (lunghezza: {len(text)}).")
                        return {'text': text, 'language': lang, 'type': 'auto'}
                    # else: logger.warning(f"[{video_id}] Fetch testo automatico '{lang}' fallito o testo vuoto.")
                except NoTranscriptFound:
                    pass # logger.debug(f"[{video_id}] Nessuna trascrizione automatica trovata per '{lang}'.")
                except Exception as e_find_auto:
                     logger.warning(f"[{video_id}] Errore durante find_generated_transcript per '{lang}': {e_find_auto}")
            
            # Fallback: qualsiasi manuale
            # logger.warning(f"[{video_id}] Provo qualsiasi manuale di fallback...")
            try:
                # --- CORREZIONE QUI ---
                transcript_obj = transcript_list.find_manually_created_transcript([]) # Passa lista vuota
                lang_found = transcript_obj.language
                # logger.info(f"[{video_id}] Trovata trascrizione MANUALE di fallback in '{lang_found}'. Recupero testo...")
                text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                if text:
                    logger.info(f"[{video_id}] Testo manuale fallback '{lang_found}' recuperato (lunghezza: {len(text)}).")
                    return {'text': text, 'language': lang_found, 'type': 'manual'}
                # else: logger.warning(f"[{video_id}] Fetch testo manuale fallback '{lang_found}' fallito o testo vuoto.")
            except NoTranscriptFound:
                pass # logger.info(f"[{video_id}] Nessuna trascrizione manuale di fallback trovata.")
            except Exception as e_find_manual_fb:
                 logger.warning(f"[{video_id}] Errore durante find_manually_created_transcript fallback: {e_find_manual_fb}")


            # Fallback: qualsiasi automatica
            # logger.warning(f"[{video_id}] Provo qualsiasi automatica di fallback...")
            try:
                # --- CORREZIONE QUI ---
                transcript_obj = transcript_list.find_generated_transcript([]) # Passa lista vuota
                lang_found = transcript_obj.language
                # logger.info(f"[{video_id}] Trovata trascrizione AUTOMATICA di fallback in '{lang_found}'. Recupero testo...")
                text = TranscriptService._fetch_and_join_transcript(transcript_obj)
                if text:
                    logger.info(f"[{video_id}] Testo automatico fallback '{lang_found}' recuperato (lunghezza: {len(text)}).")
                    return {'text': text, 'language': lang_found, 'type': 'auto'}
                # else: logger.warning(f"[{video_id}] Fetch testo automatico fallback '{lang_found}' fallito o testo vuoto.")
            except NoTranscriptFound:
                logger.warning(f"[{video_id}] NESSUNA TRASCRIZIONE (manuale o auto, fallback) trovata in NESSUNA lingua.")
                return None 
            except Exception as e_find_auto_fb:
                 logger.warning(f"[{video_id}] Errore durante find_generated_transcript fallback: {e_find_auto_fb}")


        except TranscriptsDisabled:
            logger.warning(f"[{video_id}] ERRORE: Le trascrizioni sono disabilitate per questo video.")
            return None
        except Exception as e: 
            logger.error(f"[{video_id}] ERRORE IMPREVISTO (livello get_transcript) durante il recupero della trascrizione: {str(e)}", exc_info=True) # Mantenuto exc_info=True per errori a questo livello
            return None
        
        # Se nessun return è stato eseguito prima (es. tutti i find falliscono o testo è None)
        logger.warning(f"[{video_id}] Nessuna trascrizione valida trovata dopo tutti i tentativi.")
        return None