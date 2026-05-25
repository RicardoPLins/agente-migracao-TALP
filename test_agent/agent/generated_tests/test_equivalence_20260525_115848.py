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
    migrated_scraper = MigratedConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")

    original_data = original_scraper.generateRequestData()
    migrated_data = migrated_scraper.generateRequestData()

    original_parsed = urllib.parse.parse_qs(original_data.decode('utf-8'))
    assert original_parsed == migrated_data

def test_executeRequest():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")

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

    with patch('urllib.request.urlopen', return_value=mock_resp):
        original_response = original_scraper.executeRequest(original_scraper.generateRequestData())

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=json.dumps(PAYLOAD), status=200)
        migrated_response = migrated_scraper.executeRequest(migrated_scraper.generateRequestData())

    assert original_response == json.dumps(PAYLOAD)
    assert migrated_response == json.dumps(PAYLOAD)

def test_executeRequest_network_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")

    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("reason")):
        with pytest.raises(urllib.error.URLError):
            original_scraper.executeRequest(original_scraper.generateRequestData())

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 body=requests.exceptions.ConnectionError())
        with pytest.raises(requests.exceptions.ConnectionError):
            migrated_scraper.executeRequest(migrated_scraper.generateRequestData())

def test_executeRequest_http_error():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")

    with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError("url", 404, "msg", {}, None)):
        original_response = original_scraper.executeRequest(original_scraper.generateRequestData())

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, 'https://www.facebook.com/ajax/mercury/thread_info.php',
                 status=404)
        response = migrated_scraper.executeRequest(migrated_scraper.generateRequestData())
        with pytest.raises(requests.exceptions.HTTPError):
            response.raise_for_status()

def test_init():
    original_scraper = OriginalConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")
    migrated_scraper = MigratedConversationScraper("convID", 0, 2000, "cookie", "fb_dtsg", "userID", "outDir")

    assert original_scraper._directory == "outDir/convID/"
    assert migrated_scraper._directory == "outDir/convID/"