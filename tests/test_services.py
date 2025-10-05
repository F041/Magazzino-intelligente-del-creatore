import pytest
from unittest.mock import patch, MagicMock, mock_open
import io
import xml.etree.ElementTree as ET
from google.api_core import exceptions as google_exceptions
from app.services.embedding.gemini_embedding import get_gemini_embeddings, TASK_TYPE_DOCUMENT

# Importa le funzioni e classi da testare DAI LORO MODULI ORIGINALI
from app.services.embedding.gemini_embedding import split_text_into_chunks
from app.api.routes.documents import extract_text_from_file # Assumendo sia qui
from app.services.transcripts.youtube_transcript import TranscriptService
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

# --- Test per split_text_into_chunks ---

def test_split_text_empty_string():
    """Testa con una stringa vuota."""
    assert split_text_into_chunks("", chunk_size=100, chunk_overlap=10) == []

def test_split_text_shorter_than_chunk_size():
    """Testa con un testo più corto della dimensione del chunk."""
    text = "Questo è un testo breve."
    expected_chunks = ["Questo è un testo breve."]
    assert split_text_into_chunks(text, chunk_size=100, chunk_overlap=10) == expected_chunks

def test_split_text_exact_chunk_size_no_overlap():
    """Testa con un testo che è esattamente un multiplo della dimensione del chunk, senza overlap."""
    text = "uno due tre quattro cinque sei sette otto nove dieci" # 10 parole
    # chunk_size=5 parole, chunk_overlap=0
    expected_chunks = [
        "uno due tre quattro cinque",
        "sei sette otto nove dieci"
    ]
    assert split_text_into_chunks(text, chunk_size=5, chunk_overlap=0) == expected_chunks

def test_split_text_with_overlap():
    """Testa il chunking con overlap."""
    text = "uno due tre quattro cinque sei sette otto nove dieci undici dodici" # 12 parole
    # chunk_size=5 parole, chunk_overlap=2 parole
    # Chunk 1: uno due tre quattro cinque (parole 0-4)
    # Prossimo start: 5 - 2 = 3
    # Chunk 2: quattro cinque sei sette otto (parole 3-7)
    # Prossimo start: 3 + (5 - 2) = 6
    # Chunk 3: sette otto nove dieci undici (parole 6-10)
    # Prossimo start: 6 + (5 - 2) = 9
    # Chunk 4: dieci undici dodici (parole 9-11)
    expected_chunks = [
        "uno due tre quattro cinque",
        "quattro cinque sei sette otto",
        "sette otto nove dieci undici",
        "dieci undici dodici"
    ]
    assert split_text_into_chunks(text, chunk_size=5, chunk_overlap=2) == expected_chunks

def test_split_text_large_text_with_overlap():
    """Testa con un testo più lungo e overlap."""
    words = [f"parola{i}" for i in range(1, 21)] # 20 parole
    text = " ".join(words)
    # chunk_size=7, chunk_overlap=3
    # C1: p1 p2 p3 p4 p5 p6 p7 (0-6)
    # next_start = 7-3 = 4
    # C2: p5 p6 p7 p8 p9 p10 p11 (4-10)
    # next_start = 4 + (7-3) = 8
    # C3: p9 p10 p11 p12 p13 p14 p15 (8-14)
    # next_start = 8 + (7-3) = 12
    # C4: p13 p14 p15 p16 p17 p18 p19 (12-18)
    # next_start = 12 + (7-3) = 16
    # C5: p17 p18 p19 p20 (16-19)
    expected_chunks = [
        "parola1 parola2 parola3 parola4 parola5 parola6 parola7",
        "parola5 parola6 parola7 parola8 parola9 parola10 parola11",
        "parola9 parola10 parola11 parola12 parola13 parola14 parola15",
        "parola13 parola14 parola15 parola16 parola17 parola18 parola19",
        "parola17 parola18 parola19 parola20"
    ]
    assert split_text_into_chunks(text, chunk_size=7, chunk_overlap=3) == expected_chunks

def test_split_text_overlap_greater_equal_chunk_size():
    """Testa il caso in cui l'overlap è >= chunk_size (la funzione dovrebbe correggerlo)."""
    text = "uno due tre quattro cinque sei sette otto nove dieci"
    # La funzione dovrebbe internamente ridurre l'overlap se >= chunk_size
    # Se chunk_size=3, overlap=3, overlap_corretto = 3 // 3 = 1
    # Step = 3 - 1 = 2
    # C1: uno due tre (0-2)
    # C2: tre quattro cinque (2-4)
    # C3: cinque sei sette (4-6)
    # C4: sette otto nove (6-8)
    # C5: nove dieci (8-9)
    expected_chunks_corrected_overlap = [ # Basato su overlap corretto a chunk_size // 3
        "uno due tre",
        "tre quattro cinque", # Se step è 3 - (3//3) = 2.
        "cinque sei sette",
        "sette otto nove",
        "nove dieci"
    ]
    assert split_text_into_chunks(text, chunk_size=3, chunk_overlap=3) == expected_chunks_corrected_overlap
    assert split_text_into_chunks(text, chunk_size=3, chunk_overlap=4) == expected_chunks_corrected_overlap # Anche con overlap > size


def test_split_text_invalid_chunk_size_or_overlap_uses_defaults_or_corrected():
    """Testa che la funzione gestisca chunk_size/overlap invalidi usando i default o valori corretti."""
    text = " ".join([f"w{i}" for i in range(500)]) # Testo lungo

    # Test con chunk_size <= 0 (dovrebbe usare default 300, overlap 50)
    chunks_invalid_size = split_text_into_chunks(text, chunk_size=0, chunk_overlap=10)
    # Non possiamo verificare il numero esatto di chunk facilmente senza ricalcolare,
    # ma verifichiamo che produca *qualcosa* e che il primo chunk sia lungo circa 300 parole
    assert len(chunks_invalid_size) > 0
    assert len(chunks_invalid_size[0].split()) <= 300 # Il default è 300

    # Test con overlap < 0 (dovrebbe usare default 50 per overlap)
    chunks_invalid_overlap = split_text_into_chunks(text, chunk_size=100, chunk_overlap=-5)
    assert len(chunks_invalid_overlap) > 0
    # Il primo chunk avrà chunk_size parole, l'overlap influenza i successivi
    assert len(chunks_invalid_overlap[0].split()) <= 100
    if len(chunks_invalid_overlap) > 1:
        # Verifichiamo che ci sia un overlap "sensato" (non negativo, e il default è 50)
        # Questo test è un po' euristico sull'effetto dell'overlap
        common_words = set(chunks_invalid_overlap[0].split()) & set(chunks_invalid_overlap[1].split())
        assert len(common_words) > 0 and len(common_words) <= 50 # Aspettandoci l'overlap di default (50) o meno

def test_extract_text_from_txt_file():
    """Testa l'estrazione da un file TXT."""
    # Simula un oggetto FileStorage come quello che Flask riceve
    mock_file_content = "Contenuto semplice del file TXT.\nSeconda riga."
    mock_file_storage = MagicMock()
    mock_file_storage.filename = "test.txt"
    # Per i file di testo, extract_text_from_file usa file_storage.read().decode()
    # Quindi mockhiamo .read() per restituire i bytes del nostro contenuto
    mock_file_storage.read.return_value = mock_file_content.encode('utf-8')
    
    # Non serve current_app per questa funzione unitaria se non usa config globali
    extracted_text = extract_text_from_file(mock_file_storage, "test.txt")
    
    expected_text = "Contenuto semplice del file TXT.\nSeconda riga." # La tua funzione fa strip per riga
    assert extracted_text.strip() == expected_text.strip() # Confronta dopo uno strip generale

def test_extract_text_from_pdf_file_mocked():
    """Testa l'estrazione da un file PDF mockando PdfReader."""
    mock_file_storage_pdf = MagicMock()
    mock_file_storage_pdf.filename = "test.pdf"
    # .stream è l'attributo che python-docx e pypdf usano per accedere al contenuto del file
    mock_file_storage_pdf.stream = io.BytesIO(b"dummy pdf content") # Contenuto fittizio, non verrà letto

    # Testo che ci aspettiamo dalle pagine mockate
    mock_page1_text = "Testo dalla pagina 1 del PDF."
    mock_page2_text = "Testo dalla pagina 2 del PDF, con più parole."
    
    # Crea mock per le singole pagine
    mock_pdf_page1 = MagicMock()
    mock_pdf_page1.extract_text.return_value = mock_page1_text
    
    mock_pdf_page2 = MagicMock()
    mock_pdf_page2.extract_text.return_value = mock_page2_text

    # Mock dell'istanza di PdfReader
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page1, mock_pdf_page2] # Simula una lista di oggetti pagina

    # Path per patchare PdfReader nel modulo documents.py
    # Assumendo: from pypdf import PdfReader in app/api/routes/documents.py
    path_to_pdfreader_in_documents_module = 'app.api.routes.documents.PdfReader'

    with patch(path_to_pdfreader_in_documents_module, return_value=mock_pdf_reader_instance) as MockPdfReaderClass:
        extracted_text = extract_text_from_file(mock_file_storage_pdf, "test.pdf")

        # Verifica che PdfReader sia stato chiamato con lo stream del file
        MockPdfReaderClass.assert_called_once_with(mock_file_storage_pdf.stream)
        
        # Verifica che extract_text sia stato chiamato per ogni pagina mockata
        mock_pdf_page1.extract_text.assert_called_once()
        mock_pdf_page2.extract_text.assert_called_once()

        expected_text = f"{mock_page1_text}\n\n{mock_page2_text}" # La tua funzione unisce con \n\n e poi fa strip per riga
        
        # La tua funzione extract_text_from_file fa anche '\n'.join(line.strip() ... )
        # Quindi dobbiamo normalizzare l'expected_text allo stesso modo per il confronto
        normalized_expected_text = '\n'.join(line.strip() for line in expected_text.splitlines() if line.strip())
        normalized_extracted_text = '\n'.join(line.strip() for line in extracted_text.splitlines() if line.strip())
        
        assert normalized_extracted_text == normalized_expected_text

def test_extract_text_from_unsupported_file():
    """Testa con un tipo di file non supportato."""
    mock_file_storage_unsupported = MagicMock()
    mock_file_storage_unsupported.filename = "test.zip"
    mock_file_storage_unsupported.read.return_value = b"dummy zip content"
    
    extracted_text = extract_text_from_file(mock_file_storage_unsupported, "test.zip")
    assert extracted_text is None

def test_get_gemini_embeddings_handles_rate_limit_and_fails_gracefully(monkeypatch):
    """
    TEST: Verifica che la funzione di embedding gestisca gli errori di rate limit
    facendo dei tentativi e poi fallendo in modo pulito.
    """
    # 1. ARRANGE: Prepariamo lo scenario di fallimento
    
    # Diciamo al monkeypatch di "fermare" la funzione time.sleep per rendere il test istantaneo
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    
    # Creiamo un "attore finto" che, quando chiamato, simulerà sempre un errore di "risorse esaurite"
    mock_genai = MagicMock()
    mock_genai.embed_content.side_effect = google_exceptions.ResourceExhausted("Simulated Rate Limit Exceeded")
    
    # Sostituiamo il vero 'genai' con il nostro attore finto
    with patch('app.services.embedding.gemini_embedding.genai', mock_genai):
        
        # 2. ACT: Eseguiamo la funzione che vogliamo testare
        result = get_gemini_embeddings(
            texts=["testo che fallirà"],
            api_key="fake_api_key",
            model_name="fake_model"
        )

    # 3. ASSERT: Verifichiamo il comportamento
    
    # La funzione deve restituire None per segnalare il fallimento
    assert result is None
    
    # La cosa più importante: verifichiamo che abbia provato più volte!
    # La nostra funzione è impostata per fare 5 tentativi (retries=5)
    assert mock_genai.embed_content.call_count == 5
