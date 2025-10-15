import pytest
from unittest.mock import patch, MagicMock, mock_open
import io
import xml.etree.ElementTree as ET
from google.api_core import exceptions as google_exceptions
from app.services.embedding.gemini_embedding import get_gemini_embeddings, TASK_TYPE_DOCUMENT

# Importa le funzioni e classi da testare DAI LORO MODULI ORIGINALI
from app.services.embedding.gemini_embedding import split_text_into_chunks
from app.api.routes.documents import extract_text_from_file
from app.services.transcripts.youtube_transcript import TranscriptService
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from app.services.transcripts.youtube_transcript_unofficial_library import UnofficialTranscriptService

# --- Test invariati e corretti ---

def test_split_text_empty_string():
    assert split_text_into_chunks("", chunk_size=100, chunk_overlap=10) == []

def test_split_text_shorter_than_chunk_size():
    text = "Questo è un testo breve."
    expected_chunks = ["Questo è un testo breve."]
    assert split_text_into_chunks(text, chunk_size=100, chunk_overlap=10) == expected_chunks

def test_split_text_exact_chunk_size_no_overlap():
    text = "uno due tre quattro cinque sei sette otto nove dieci"
    expected_chunks = ["uno due tre quattro cinque", "sei sette otto nove dieci"]
    assert split_text_into_chunks(text, chunk_size=5, chunk_overlap=0) == expected_chunks

def test_split_text_with_overlap():
    text = "uno due tre quattro cinque sei sette otto nove dieci undici dodici"
    expected_chunks = [
        "uno due tre quattro cinque",
        "quattro cinque sei sette otto",
        "sette otto nove dieci undici",
        "dieci undici dodici"
    ]
    assert split_text_into_chunks(text, chunk_size=5, chunk_overlap=2) == expected_chunks

def test_get_gemini_embeddings_handles_rate_limit_and_fails_gracefully(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    mock_genai = MagicMock()
    mock_genai.embed_content.side_effect = google_exceptions.ResourceExhausted("Simulated Rate Limit Exceeded")
    with patch('app.services.embedding.gemini_embedding.genai', mock_genai):
        result = get_gemini_embeddings(
            texts=["testo che fallirà"],
            api_key="fake_api_key",
            model_name="fake_model"
        )
    assert result is None
    assert mock_genai.embed_content.call_count == 5

def test_unofficial_transcript_service_chooses_correct_strategy(monkeypatch):
    mock_transcript_object = MagicMock(is_generated=False, language_code='it')
    mock_transcript_object.fetch.return_value = [MagicMock(text='testo moderno')]
    
    mock_transcript_list = MagicMock()
    mock_transcript_list.find_transcript.return_value = mock_transcript_object
    
    mock_api_instance = MagicMock()
    mock_api_instance.list.return_value = mock_transcript_list
    
    path_to_api_class = 'app.services.transcripts.youtube_transcript_unofficial_library.YouTubeTranscriptApi'
    with patch(path_to_api_class, return_value=mock_api_instance) as mock_api_class_constructor:
        
        result = UnofficialTranscriptService.get_transcript('video1')

    assert result['text'] == 'testo moderno'
    assert result['type'] == 'manual'
    mock_api_class_constructor.assert_called_once()
    mock_api_instance.list.assert_called_once_with('video1')

# --- Test per extract_text_from_file ---

def test_extract_text_from_txt_file():
    mock_file_content = "Contenuto semplice del file TXT.\\nSeconda riga."
    mock_file_storage = MagicMock()
    mock_file_storage.read.return_value = mock_file_content.encode('utf-8')
    extracted_text = extract_text_from_file(mock_file_storage, "test.txt")
    expected_text = "Contenuto semplice del file TXT.\\nSeconda riga."
    assert extracted_text.strip() == expected_text.strip()

def test_extract_text_from_pdf_file_mocked():
    """Testa l'estrazione da un file PDF con una logica di verifica robusta."""
    # 1. ARRANGE
    mock_file_storage_pdf = MagicMock()
    mock_file_storage_pdf.stream = io.BytesIO(b"dummy pdf content")
    
    mock_page1_text = "Testo dalla pagina 1 del PDF."
    mock_page2_text = "Testo dalla pagina 2 del PDF, con più parole."
    
    mock_pdf_page1 = MagicMock()
    mock_pdf_page1.extract_text.return_value = mock_page1_text
    
    mock_pdf_page2 = MagicMock()
    mock_pdf_page2.extract_text.return_value = mock_page2_text
    
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page1, mock_pdf_page2]
    
    path_to_pdfreader = 'app.api.routes.documents.PdfReader'

    with patch(path_to_pdfreader, return_value=mock_pdf_reader_instance):
        # 2. ACT
        extracted_text = extract_text_from_file(mock_file_storage_pdf, "test.pdf")

    # 3. ASSERT (LOGICA ROBUSTA E CORRETTA)
    assert extracted_text is not None
    
    # Dividiamo il risultato in righe usando il carattere "a capo" corretto.
    lines = extracted_text.split('\n')
    
    assert len(lines) == 2, "Il testo estratto dovrebbe avere esattamente due righe"
    assert lines[0] == mock_page1_text, "La prima riga non corrisponde al testo della pagina 1"
    assert lines[1] == mock_page2_text, "La seconda riga non corrisponde al testo della pagina 2"

def test_extract_text_from_unsupported_file():
    mock_file_storage_unsupported = MagicMock()
    mock_file_storage_unsupported.read.return_value = b"dummy zip content"
    extracted_text = extract_text_from_file(mock_file_storage_unsupported, "test.zip")
    assert extracted_text is None