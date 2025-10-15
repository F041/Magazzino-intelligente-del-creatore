import pytest
from unittest.mock import patch, MagicMock
from app.services.transcripts.youtube_transcript_unofficial_library import UnofficialTranscriptService
from app.services.youtube.client import YouTubeClient
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
import sqlite3
from app.core.youtube_processor import _process_youtube_channel_core
from app.api.models.video import Video

# Definiamo il percorso della CLASSE che vogliamo "ingannare"
YOUTUBE_TRANSCRIPT_API_PATH = 'app.services.transcripts.youtube_transcript_unofficial_library.YouTubeTranscriptApi'

def test_unofficial_transcript_success():
    """
    TEST SCENARIO 1: Verifica il caso di successo con la nuova sintassi della libreria.
    """
    # 1. ARRANGE: Prepariamo le risposte finte
    mock_transcript_object = MagicMock()
    mock_transcript_object.is_generated = False
    mock_transcript_object.language_code = 'it'
    # fetch() ora restituisce oggetti con l'attributo .text
    mock_transcript_object.fetch.return_value = [MagicMock(text='Ciao mondo'), MagicMock(text='seconda frase.')]
    
    mock_transcript_list = MagicMock()
    mock_transcript_list.find_transcript.return_value = mock_transcript_object
    
    # Creiamo un'istanza finta dell'API che verrà restituita quando il nostro codice chiama YouTubeTranscriptApi()
    mock_api_instance = MagicMock()
    mock_api_instance.list.return_value = mock_transcript_list
    
    with patch(YOUTUBE_TRANSCRIPT_API_PATH, return_value=mock_api_instance) as mock_api_class:
        # 2. ACT: Eseguiamo la nostra funzione
        result = UnofficialTranscriptService.get_transcript('video_id_success')

    # 3. ASSERT: Verifichiamo il risultato
    assert result is not None
    assert result['text'] == 'Ciao mondo seconda frase.'
    assert result['language'] == 'it'
    assert result['type'] == 'manual'
    assert 'error' not in result
    mock_api_class.assert_called_once() # Verifica che l'istanza sia stata creata
    mock_api_instance.list.assert_called_once_with('video_id_success') # Verifica che il metodo .list() sia stato chiamato

def test_unofficial_transcript_transcripts_disabled():
    """
    TEST SCENARIO 2: Simula il caso in cui le trascrizioni sono disabilitate.
    """
    # 1. ARRANGE: Configuriamo la nostra istanza finta per generare un errore
    mock_api_instance = MagicMock()
    mock_api_instance.list.side_effect = TranscriptsDisabled('video_id_disabled')

    with patch(YOUTUBE_TRANSCRIPT_API_PATH, return_value=mock_api_instance):
        # 2. ACT
        result = UnofficialTranscriptService.get_transcript('video_id_disabled')

    # 3. ASSERT: Verifichiamo che l'errore sia stato gestito
    assert result['error'] == 'TRANSCRIPTS_DISABLED'
    assert 'disabilitate' in result['message']

def test_unofficial_transcript_ip_blocked_error():
    """
    TEST SCENARIO 3: Simula l'errore di blocco IP.
    """
    # 1. ARRANGE: L'errore viene sollevato da .list()
    error_message = "HTTP Error 429: Too Many Requests"
    mock_api_instance = MagicMock()
    mock_api_instance.list.side_effect = Exception(error_message)

    with patch(YOUTUBE_TRANSCRIPT_API_PATH, return_value=mock_api_instance):
        # 2. ACT
        result = UnofficialTranscriptService.get_transcript('video_id_blocked')

    # 3. ASSERT: Verifichiamo che il codice abbia riconosciuto l'errore
    assert result['error'] == 'IP_BLOCKED'
    assert 'bloccato' in result['message']

# Il resto dei test in questo file non era fallito, ma li lascio per completezza e coerenza
@pytest.mark.parametrize("url_input, expected_id", [
    ("UCJSuTw2VDoX0CWejniSo5TA", "UCJSuTw2VDoX0CWejniSo5TA"),
    ("https://www.youtube.com/channel/UCJSuTw2VDoX0CWejniSo5TA", "UCJSuTw2VDoX0CWejniSo5TA"),
    ("https://www.youtube.com/@STATiCalmo", "UCJSuTw2VDoX0CWejniSo5TA"),
])
def test_youtube_client_extract_channel_info(url_input, expected_id, monkeypatch):
    mock_youtube_api = MagicMock()
    search_response = {
        'items': [{'id': {'channelId': 'UCJSuTw2VDoX0CWejniSo5TA'}}]
    }
    mock_youtube_api.search.return_value.list.return_value.execute.return_value = search_response
    
    monkeypatch.setattr("app.services.youtube.client.YouTubeClient._init_service", lambda self: None)
    client = YouTubeClient(token_file="dummy_token.json")
    client.youtube = mock_youtube_api
    
    extracted_id = client.extract_channel_info(url_or_id=url_input)
    
    assert extracted_id == expected_id

def test_get_channel_videos_handles_pagination_alternative_mock(monkeypatch):
    mock_response_page1 = {
        'pageInfo': {'totalResults': 2},
        'items': [{'id': {'videoId': 'vid1', 'kind': 'youtube#video'}, 'snippet': {'title': 'Video 1', 'publishedAt': '2023-01-01T00:00:00Z', 'description': 'Desc 1'}}],
        'nextPageToken': 'token_for_page2'
    }
    mock_response_page2 = {
        'pageInfo': {'totalResults': 2},
        'items': [{'id': {'videoId': 'vid2', 'kind': 'youtube#video'}, 'snippet': {'title': 'Video 2', 'publishedAt': '2023-01-02T00:00:00Z', 'description': 'Desc 2'}}]
    }

    mock_youtube_service = MagicMock()
    list_method_mock = mock_youtube_service.search.return_value.list
    list_method_mock.return_value.execute.side_effect = [
        mock_response_page1, 
        mock_response_page2
    ]

    path_to_build = 'app.services.youtube.client.build'
    
    with patch(path_to_build, return_value=mock_youtube_service):
        monkeypatch.setattr("app.services.youtube.client.Credentials.from_authorized_user_file", lambda *args, **kwargs: MagicMock())
        monkeypatch.setattr("os.path.exists", lambda path: True)
        
        client = YouTubeClient(token_file="dummy_token.json")
        videos, total_count = client.get_channel_videos_and_total_count(channel_id="fake_channel")

    assert total_count == 2
    assert len(videos) == 2
    assert videos[0].video_id == 'vid1'
    assert videos[1].video_id == 'vid2'
    assert list_method_mock.return_value.execute.call_count == 2

def test_youtube_processor_core_logic(app, monkeypatch):
    user_id_test = "user_for_processor_test"
    with app.app_context():
        db_path = app.config['DATABASE_FILE']
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM videos WHERE user_id=?", (user_id_test,))
        cursor.execute(
            "INSERT INTO videos (video_id, title, user_id, channel_id, published_at, url, processing_status) VALUES (?,?,?,?,?,?,?)",
            ("vid_esistente", "Video Vecchio", user_id_test, "channel_test", "2023-01-01T00:00:00Z", "http://fake.url", "completed")
        )
        conn.commit()
        conn.close()

    videos_from_yt_api = [
        Video(video_id="vid_esistente", title="Video Vecchio", channel_id="channel_test", published_at="2023-01-01T00:00:00Z", url="..."),
        Video(video_id="vid_nuovo", title="Video Nuovo", channel_id="channel_test", published_at="2023-01-02T00:00:00Z", url="...")
    ]

    mock_transcript_result = {'text': 'Questa e la trascrizione del video nuovo.', 'language': 'it', 'type': 'auto'}
    mock_chunks = ['Questa e la trascrizione del video nuovo.']
    mock_embeddings = [[0.1]*768]
    
    mock_core_config = {**app.config, 'APP_MODE': 'saas', 'CHROMA_CLIENT': MagicMock(), 'VIDEO_COLLECTION_NAME': 'video_transcripts'}
    mock_chroma_collection = MagicMock()
    mock_core_config['CHROMA_CLIENT'].get_or_create_collection.return_value = mock_chroma_collection

    path_unofficial_transcript = 'app.core.youtube_processor.UnofficialTranscriptService.get_transcript'
    path_official_transcript = 'app.core.youtube_processor.TranscriptService.get_transcript'
    path_youtube_client = 'app.core.youtube_processor.YouTubeClient'
    path_split_chunks = 'app.core.youtube_processor.split_text_into_chunks'
    path_generate_embeddings = 'app.core.youtube_processor.generate_embeddings'

    with patch(path_unofficial_transcript, return_value=mock_transcript_result) as mock_unofficial, \
         patch(path_official_transcript) as mock_official, \
         patch(path_youtube_client, MagicMock()), \
         patch(path_split_chunks, return_value=mock_chunks), \
         patch(path_generate_embeddings, return_value=mock_embeddings):

        status_dict_fake = {}
        with app.app_context():
            result = _process_youtube_channel_core(
                channel_id="channel_test",
                user_id=user_id_test,
                core_config=mock_core_config,
                videos_from_yt_models=videos_from_yt_api,
                status_dict=status_dict_fake,
                use_official_api_only=False
            )

    assert result['success'] is True
    assert result['new_videos_processed'] == 1
    mock_unofficial.assert_called_once_with('vid_nuovo')
    mock_official.assert_not_called()
    mock_core_config['CHROMA_CLIENT'].get_or_create_collection.assert_called_once()
    mock_chroma_collection.upsert.assert_called_once()
    
    video_salvato = None
    with app.app_context():
        conn = sqlite3.connect(app.config['DATABASE_FILE'])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM videos WHERE video_id = ?", ("vid_nuovo",))
        video_salvato = cursor.fetchone()
        conn.close()

    assert video_salvato is not None
    assert video_salvato['processing_status'] == 'completed'
    assert video_salvato['transcript'] == 'Questa e la trascrizione del video nuovo.'