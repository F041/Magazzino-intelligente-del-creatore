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
    youtube_transcript_api. Questo metodo non consuma quote dell'API di YouTube.
    """
    
    @staticmethod
    def get_transcript(video_id: str, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        """
        Tenta di recuperare la trascrizione per un dato video_id.
        Cerca prima nelle lingue preferite, poi tenta un fallback su qualsiasi lingua disponibile.
        """
        logger.info(f"[Unofficial Lib] Avvio recupero trascrizione per video ID: {video_id}")
        
        try:
            # 1. Ottieni la lista delle trascrizioni disponibili
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            
            # 2. Cerca la trascrizione nelle lingue preferite
            transcript_to_fetch = None
            found_lang_code = None
            
            for lang_code in preferred_languages:
                try:
                    transcript_to_fetch = transcript_list.find_transcript([lang_code])
                    found_lang_code = lang_code
                    logger.info(f"[Unofficial Lib] Trovata trascrizione preferita in '{lang_code}' per {video_id}.")
                    break
                except NoTranscriptFound:
                    continue # Continua a cercare nella prossima lingua preferita
            
            # 3. Se non trovata, prova un fallback su qualsiasi lingua generabile automaticamente
            if not transcript_to_fetch:
                try:
                    transcript_to_fetch = transcript_list.find_generated_transcript(preferred_languages)
                    found_lang_code = transcript_to_fetch.language_code
                    logger.info(f"[Unofficial Lib] Trovata trascrizione generata automaticamente in '{found_lang_code}' per {video_id}.")
                except NoTranscriptFound:
                     # Come ultima spiaggia, prendi la prima trascrizione disponibile
                    logger.warning(f"[Unofficial Lib] Nessuna trascrizione preferita o generata trovata. Tento fallback sulla prima disponibile.")
                    first_transcript = next(iter(transcript_list), None)
                    if first_transcript:
                        transcript_to_fetch = first_transcript
                        found_lang_code = first_transcript.language_code

            if not transcript_to_fetch:
                # Questo caso è raro se la lista non è vuota, ma per sicurezza
                raise NoTranscriptFound(video_id)

            # 4. Scarica e formatta il testo
            transcript_pieces = transcript_to_fetch.fetch()
            full_transcript_text = " ".join(piece['text'] for piece in transcript_pieces)
            
            # Determina il tipo di trascrizione
            caption_type = 'manual' if not transcript_to_fetch.is_generated else 'auto'
            
            logger.info(f"[Unofficial Lib] Trascrizione recuperata con successo per {video_id} (Lingua: {found_lang_code}, Tipo: {caption_type}).")
            
            return {
                'text': full_transcript_text,
                'language': found_lang_code,
                'type': caption_type
            }
        
        except TranscriptsDisabled:
            logger.warning(f"[Unofficial Lib] Le trascrizioni sono disabilitate per il video {video_id}.")
            return {'error': 'TRANSCRIPTS_DISABLED', 'message': 'Le trascrizioni sono disabilitate per questo video.'}
        except NoTranscriptFound:
            logger.warning(f"[Unofficial Lib] Nessuna trascrizione trovata per il video {video_id}.")
            return {'error': 'NO_TRANSCRIPT_FOUND', 'message': 'Nessuna trascrizione disponibile per questo video.'}
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            # Questa parte rimane uguale
            message = f"Le trascrizioni sono disabilitate per il video {video_id}." if isinstance(e, TranscriptsDisabled) else f"Nessuna trascrizione trovata per il video {video_id}."
            error_code = 'TRANSCRIPTS_DISABLED' if isinstance(e, TranscriptsDisabled) else 'NO_TRANSCRIPT_FOUND'
            logger.warning(f"[Unofficial Lib] {message}")
            return {'error': error_code, 'message': message}
        except ET.ParseError as e_xml: # <-- NUOVO BLOCCO
            # Gestiamo specificamente l'errore di parsing
            logger.warning(f"[Unofficial Lib] Errore parsing XML per {video_id} (probabile risposta vuota da YouTube): {e_xml}")
            return {'error': 'XML_PARSE_ERROR', 'message': 'YouTube ha restituito una risposta vuota o malformattata.'}
        except Exception as e:
            # Ispezioniamo il testo dell'errore per vedere se è un blocco IP (HTTP 429)
            error_text = str(e).lower()
            if 'http error 429' in error_text:
                logger.error(f"[Unofficial Lib] RILEVATO BLOCCO IP (HTTP 429) per il video {video_id}. Dettagli: {e}")
                return {'error': 'IP_BLOCKED', 'message': 'YouTube ha temporaneamente bloccato l\'indirizzo IP del server. È necessario usare l\'API ufficiale.'}
            
            # Se non è un errore 429, lo gestiamo come un errore generico
            logger.error(f"[Unofficial Lib] Errore imprevisto durante il recupero della trascrizione per {video_id}: {e}", exc_info=True)
            return {'error': 'UNKNOWN_ERROR', 'message': f'Errore imprevisto: {e}'}