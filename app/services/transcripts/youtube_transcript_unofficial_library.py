# FILE: app/services/transcripts/youtube_transcript_unofficial_library.py
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound 
from typing import Optional, Dict
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)

class UnofficialTranscriptService:
    """
    Servizio per recuperare trascrizioni utilizzando la libreria non ufficiale
    youtube_transcript_api, compatibile con la versione 1.2.3+.
    """
    
    @staticmethod
    def get_transcript(video_id: str, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        """
        Tenta di recuperare la trascrizione per un dato video_id.
        """
        logger.info(f"[Unofficial Lib] Avvio recupero trascrizione per video ID: {video_id}")
        
        try:
            api = YouTubeTranscriptApi()
            transcript_list = api.list(video_id)
            
            transcript_to_fetch = None
            found_lang_code = None
            
            # Cerca la trascrizione nelle lingue preferite
            for lang_code in preferred_languages:
                try:
                    transcript_to_fetch = transcript_list.find_transcript([lang_code])
                    found_lang_code = lang_code
                    logger.info(f"[Unofficial Lib] Trovata trascrizione preferita in '{lang_code}' per {video_id}.")
                    break
                except NoTranscriptFound:
                    continue
            
            # Se non trovata, prova un fallback su qualsiasi lingua generabile automaticamente
            if not transcript_to_fetch:
                try:
                    transcript_to_fetch = transcript_list.find_generated_transcript(preferred_languages)
                    found_lang_code = transcript_to_fetch.language_code
                    logger.info(f"[Unofficial Lib] Trovata trascrizione generata automaticamente in '{found_lang_code}' per {video_id}.")
                except NoTranscriptFound:
                    logger.warning(f"[Unofficial Lib] Nessuna trascrizione preferita o generata trovata. Tento fallback sulla prima disponibile.")
                    first_transcript = next(iter(transcript_list), None)
                    if first_transcript:
                        transcript_to_fetch = first_transcript
                        found_lang_code = first_transcript.language_code

            if not transcript_to_fetch:
                raise NoTranscriptFound(video_id)

            # Scarica i dati della trascrizione
            transcript_pieces = transcript_to_fetch.fetch()
            
            # --- CORREZIONE DEFINITIVA QUI ---
            # Usiamo piece.text (accesso all'attributo) invece di piece['text'] (accesso al dizionario)
            full_transcript_text = " ".join(piece.text for piece in transcript_pieces)
            
            caption_type = 'manual' if not transcript_to_fetch.is_generated else 'auto'
            
            logger.info(f"[Unofficial Lib] Trascrizione recuperata con successo per {video_id} (Lingua: {found_lang_code}, Tipo: {caption_type}).")
            
            return {
                'text': full_transcript_text,
                'language': found_lang_code,
                'type': caption_type
            }
        
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            message = f"Le trascrizioni sono disabilitate per il video {video_id}." if isinstance(e, TranscriptsDisabled) else f"Nessuna trascrizione trovata per il video {video_id}."
            error_code = 'TRANSCRIPTS_DISABLED' if isinstance(e, TranscriptsDisabled) else 'NO_TRANSCRIPT_FOUND'
            logger.warning(f"[Unofficial Lib] {message}")
            return {'error': error_code, 'message': message}
            
        except ET.ParseError as e_xml:
            logger.warning(f"[Unofficial Lib] Errore parsing XML per {video_id} (risposta vuota da YouTube): {e_xml}")
            return {'error': 'XML_PARSE_ERROR', 'message': 'YouTube ha restituito una risposta vuota o malformattata.'}
            
        except Exception as e:
            error_text = str(e).lower()
            if 'http error 429' in error_text:
                logger.error(f"[Unofficial Lib] RILEVATO BLOCCO IP (HTTP 429) per il video {video_id}. Dettagli: {e}")
                return {'error': 'IP_BLOCKED', 'message': 'YouTube ha temporaneamente bloccato l\'indirizzo IP del server. È necessario usare l\'API ufficiale.'}
            
            logger.error(f"[Unofficial Lib] Errore imprevisto per {video_id}: {e}", exc_info=True)
            return {'error': 'UNKNOWN_ERROR', 'message': f'Errore imprevisto: {e}'}