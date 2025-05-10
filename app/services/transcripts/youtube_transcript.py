from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

class TranscriptService:
    @staticmethod
    def get_transcript(video_id: str, preferred_languages: list = ['it', 'en']) -> Optional[Dict[str, str]]:
        """
        Recupera la trascrizione di un video YouTube.
        Restituisce un dizionario con il testo della trascrizione, la lingua e il tipo (manuale o automatico).
        """
        logger.info(f"Inizio recupero trascrizione per video ID: {video_id}")
        try:
            # Prova a ottenere le trascrizioni disponibili
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            logger.info(f"[{video_id}] Lista trascrizioni trovata.")

            # Prima cerca sottotitoli manuali nelle lingue preferite
            for lang in preferred_languages:
                logger.info(f"[{video_id}] Tentativo ricerca manuale per lingua: {lang}")
                try:
                    transcript = transcript_list.find_manually_created_transcript([lang])
                    logger.info(f"[{video_id}] Trovata trascrizione MANUALE in '{lang}'. Recupero testo...")
                    # --- CORREZIONE QUI ---
                    text = ' '.join([t.text for t in transcript.fetch()]) # Usa t['text'] invece di t.text
                    # --- FINE CORREZIONE ---
                    logger.info(f"[{video_id}] Testo recuperato con successo (lunghezza: {len(text)}).")
                    return {
                        'text': text,
                        'language': lang,
                        'type': 'manual'
                    }
                except NoTranscriptFound:
                    logger.info(f"[{video_id}] Nessuna trascrizione manuale trovata per '{lang}'.")
                    continue # Prova la prossima lingua preferita

            # Se non trova sottotitoli manuali nelle lingue preferite, cerca quelli automatici
            logger.info(f"[{video_id}] Nessuna trascrizione manuale trovata nelle lingue preferite {preferred_languages}. Provo con quelle automatiche...")
            for lang in preferred_languages:
                logger.info(f"[{video_id}] Tentativo ricerca automatica per lingua: {lang}")
                try:
                    transcript = transcript_list.find_generated_transcript([lang])
                    logger.info(f"[{video_id}] Trovata trascrizione AUTOMATICA in '{lang}'. Recupero testo...")
                    
                    text = ' '.join([t.text for t in transcript.fetch()]) # Usa t['text'] invece di t.text
                    
                    logger.info(f"[{video_id}] Testo recuperato con successo (lunghezza: {len(text)}).")
                    return {
                        'text': text,
                        'language': lang,
                        'type': 'auto'
                    }
                except NoTranscriptFound:
                    logger.info(f"[{video_id}] Nessuna trascrizione automatica trovata per '{lang}'.")
                    continue # Prova la prossima lingua preferita

            # Come ultima risorsa, prende qualsiasi sottotitolo manuale disponibile (in qualsiasi lingua)
            logger.warning(f"[{video_id}] Nessuna trascrizione (manuale o auto) trovata nelle lingue preferite. Provo qualsiasi manuale...")
            try:
                # Nota: find_manually_created_transcript senza argomenti cerca in tutte le lingue
                transcript = transcript_list.find_manually_created_transcript()
                lang_found = transcript.language # Prendi la lingua effettiva trovata
                logger.info(f"[{video_id}] Trovata trascrizione MANUALE di fallback in '{lang_found}'. Recupero testo...")
                # --- CORREZIONE QUI ---
                text = ' '.join([t.text for t in transcript.fetch()]) # Usa t['text'] invece di t.text
                # --- FINE CORREZIONE ---
                logger.info(f"[{video_id}] Testo recuperato con successo (lunghezza: {len(text)}).")
                return {
                    'text': text,
                    'language': lang_found,
                    'type': 'manual'
                }
            except NoTranscriptFound:
                # Se non trova NESSUN sottotitolo manuale, prova qualsiasi automatico
                logger.warning(f"[{video_id}] Nessuna trascrizione manuale trovata in nessuna lingua. Provo qualsiasi automatica...")
                try:
                     # Nota: find_generated_transcript senza argomenti cerca in tutte le lingue
                    transcript = transcript_list.find_generated_transcript()
                    lang_found = transcript.language
                    logger.info(f"[{video_id}] Trovata trascrizione AUTOMATICA di fallback in '{lang_found}'. Recupero testo...")
                    text = ' '.join([t.text for t in transcript.fetch()]) # Usa t['text'] invece di t.text
                    logger.info(f"[{video_id}] Testo recuperato con successo (lunghezza: {len(text)}).")
                    return {
                        'text': text,
                        'language': lang_found,
                        'type': 'auto'
                    }
                except NoTranscriptFound:
                    # Se arriva qui, non ha trovato assolutamente nulla
                    logger.warning(f"[{video_id}] NESSUNA TRASCRIZIONE (manuale o auto) trovata in NESSUNA lingua.")
                    return None # Restituisce None se non trova nulla

        except TranscriptsDisabled:
            logger.warning(f"[{video_id}] ERRORE: Le trascrizioni sono disabilitate per questo video.")
            return None
        except Exception as e:
            # Logga l'eccezione completa per più dettagli
            logger.exception(f"[{video_id}] ERRORE IMPREVISTO durante il recupero della trascrizione: {str(e)}")
            return None


    @staticmethod
    def check_captions_availability(video_id: str) -> bool:
        """Verifica se ci sono sottotitoli manuali disponibili per un video"""
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            # Cerca sottotitoli manuali
            try:
                transcript_list.find_manually_created_transcript()
                return True
            except NoTranscriptFound:
                return False
        except TranscriptsDisabled:
            logger.warning(f"Transcripts are disabled for video {video_id}")
            return False
        except Exception as e:
            logger.error(f"Error checking captions availability for video {video_id}: {str(e)}")
            return False 