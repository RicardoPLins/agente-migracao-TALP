import urllib.parse
import urllib.error
import urllib.request
import gzip
import io
import json
import responses
import requests
from unittest.mock import MagicMock, patch
import pytest
from original_module import ConversationScraper as OriginalConversationScraper
from migrated_module import ConversationScraper as MigratedConversationScraper

def test_generateRequestData():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")

    original_data = original_scraper.generateRequestData()
    parsed = urllib.parse.parse_qs(original_data.decode('utf-8'))
    flat = {k: v[0] for k, v in parsed.items()}

    migrated_data = migrated_scraper.generateRequestData(0, "", 2000, False)
    for key in flat:
        if key in migrated_data:
            assert flat[key] == str(migrated_data[key])

def test_executeRequest():
    PAYLOAD = {"key": "value"}
    compressed = gzip.compress(json.dumps(PAYLOAD).encode())
    buf = io.BytesIO(compressed)
    mock_resp = MagicMock()
    mock_resp.read.side_effect = buf.read
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    with patch('urllib.request.urlopen', return_value=mock_resp):
        result = original_scraper.executeRequest(original_scraper.generateRequestData())
        assert result is not None

    @responses.activate
    def test_executeRequest_migrated():
        PAYLOAD = {"key": "value"}
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     json=PAYLOAD, status=200)
        migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
        result = migrated_scraper.executeRequest(migrated_scraper.generateRequestData(0, "", 2000, False))
        assert result is not None

    test_executeRequest_migrated()

def test_scrapeConversation():
    PAYLOAD = {"key": "value"}
    compressed = gzip.compress(json.dumps(PAYLOAD).encode())
    buf = io.BytesIO(compressed)
    mock_resp = MagicMock()
    mock_resp.read.side_effect = buf.read
    mock_resp.readable.return_value = True
    mock_resp.seekable.return_value = False
    mock_resp.writable.return_value = False
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    PREFIX = "X" * 9
    with patch.object(original_scraper, 'executeRequest', return_value=PREFIX + json.dumps(PAYLOAD)):
        original_scraper.scrapeConversation(False)

    @responses.activate
    def test_scrapeConversation_migrated():
        PAYLOAD = {"key": "value"}
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     json=PAYLOAD, status=200)
        migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
        migrated_scraper.scrapeConversation(False, 0, 0, 2000, 0, False)

    test_scrapeConversation_migrated()

def test_executeRequest_network_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("reason")):
        with pytest.raises(urllib.error.URLError):
            original_scraper.executeRequest(original_scraper.generateRequestData())

    @responses.activate
    def test_executeRequest_network_error_migrated():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     body=requests.exceptions.ConnectionError())
        migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
        with pytest.raises(requests.exceptions.ConnectionError):
            migrated_scraper.executeRequest(migrated_scraper.generateRequestData(0, "", 2000, False))

    test_executeRequest_network_error_migrated()

def test_executeRequest_http_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError("url", 404, "msg", {}, None)):
        result = original_scraper.executeRequest(original_scraper.generateRequestData())
        assert result is not None

    @responses.activate
    def test_executeRequest_http_error_migrated():
        responses.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                     status=404)
        migrated_scraper = MigratedConversationScraper("convID", "cookie", "fb_dtsg", "outDir")
        result = migrated_scraper.executeRequest(migrated_scraper.generateRequestData(0, "", 2000, False))
        assert result is not None

    test_executeRequest_http_error_migrated()